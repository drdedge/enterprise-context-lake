"""Shared pytest fixtures, offline environment hygiene, and contract helpers.

Guarantees:
  - 100% offline execution (strips cloud LLM keys, forces deterministic LLM mode)
  - Strict Windows SQLite file-lock cleanup (always closes stores/engines/SDKs in finally)
  - Real implementations only (no mocking away core/adapter/API/MCP/Agent logic)
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"

# Ensure all workspace packages are importable even if invoked outside pytest pythonpath
for _pkg in ("core", "adapters", "api", "mcp", "sdk", "agents"):
    _p = str(REPO_ROOT / "packages" / _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session", autouse=True)
def enforce_offline_env() -> None:
    """Guarantee all tests run 100% offline with zero external LLM API keys."""
    os.environ["AUTH_MODE"] = "mock"
    os.environ.pop("CONTEXT_HUB_BACKEND", None)  # always the local folder emulation in tests
    os.environ["LLM_MODE"] = "deterministic"
    os.environ["CONTEXT_LAKE_OFFLINE"] = "1"
    os.environ["LANGSMITH_TRACING"] = "false"
    for secret_key in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "LANGSMITH_API_KEY",
        "CONTEXT_TRACING_LANGSMITH",
        "CONTEXT_TRACING_INTERNAL_OTLP_ENDPOINT",
        "CONTEXT_TRACE_CONTENT",
    ):
        os.environ.pop(secret_key, None)


def load_json_fixture(rel_path: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / rel_path).read_text(encoding="utf-8"))


def lf_sha256(path: Path) -> str:
    raw = path.read_bytes()
    text_exts = {".json", ".md", ".csv", ".yml", ".yaml", ".txt", ".py"}
    if path.suffix.lower() in text_exts:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def parse_utc(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def mock_headers(
    user_id: str,
    *,
    actor_mode: str = "obo",
    channel_mode: str = "direct",
    groups_overage: bool = False,
    if_match: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "X-Mock-User": user_id,
        "X-Mock-Actor-Mode": actor_mode,
        "X-Mock-Channel-Mode": channel_mode,
    }
    if groups_overage:
        headers["X-Mock-Groups-Overage"] = "true"
    if if_match is not None:
        headers["If-Match"] = if_match
    if extra:
        headers.update(extra)
    return headers


def resolve_symbol(candidates: list[tuple[str, str]]) -> Any:
    """Import the first matching (module_name, attr_name) from workspace packages."""
    errors: list[str] = []
    for mod_name, attr_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            errors.append(f"{mod_name}: {type(exc).__name__}: {exc}")
            continue
        if hasattr(mod, attr_name):
            return getattr(mod, attr_name)
        errors.append(f"{mod_name} loaded but missing attribute '{attr_name}'")
    raise ImportError(
        "Could not resolve symbol from candidates:\n  - " + "\n  - ".join(errors)
    )


def get_create_app() -> Any:
    return resolve_symbol(
        [
            ("context_api.main", "create_app"),
            ("context_api.app", "create_app"),
            ("context_api", "create_app"),
        ]
    )


def build_principal_from_fixture(
    user_id: str,
    *,
    actor_mode: str = "obo",
    channel_mode: str = "direct",
    resolve_overage: bool = True,
) -> Any:
    """Build a canonical context_core Principal model from fixtures/principals.json."""
    Principal = resolve_symbol(
        [
            ("context_core.models", "Principal"),
            ("context_core", "Principal"),
        ]
    )
    principals_doc = load_json_fixture("principals.json")
    rec = next(p for p in principals_doc["principals"] if p["user_id"] == user_id)
    groups: list[str] = list(rec.get("groups", []))
    overage_resolved = False
    if rec.get("groups_overage") and resolve_overage:
        core_groups = list(rec["directory_core_groups"])
        prefix = rec["directory_extra_group_prefix"]
        extra = [f"{prefix}{i:03d}" for i in range(1, int(rec["directory_extra_group_count"]) + 1)]
        groups = core_groups + extra
        overage_resolved = True

    return Principal(
        user_id=rec["user_id"],
        display_name=rec["display_name"],
        email=rec["email"],
        roles=frozenset(rec.get("roles", [])),
        groups=frozenset(groups),
        wall_side=rec["wall_side"],
        auth_mode="local_mock",
        actor_mode=actor_mode,
        channel_mode=channel_mode,
        revoked=bool(rec.get("revoked", False)),
        groups_overage_resolved=overage_resolved,
    )


@pytest.fixture
def principals_fixture() -> dict[str, Any]:
    return load_json_fixture("principals.json")


@pytest.fixture
async def api_app(tmp_path: Path):
    """Create an isolated seeded FastAPI app instance with guaranteed teardown."""
    create_app = get_create_app()
    db_path = tmp_path / "context_lake_test.sqlite3"
    okf_dir = tmp_path / "okf_vault"
    snap_dir = tmp_path / "snapshots"
    upload_dir = tmp_path / "uploads"

    # Pass isolated directories via supported kwargs or env fallback
    import inspect

    sig = inspect.signature(create_app)
    kwargs: dict[str, Any] = {}
    param_map = {
        "db_path": db_path,
        "runtime_dir": tmp_path,
        "repo_root": REPO_ROOT,
        "llm_mode": "deterministic",
        "auth_mode": "mock",
        "seed_fixtures": True,
        "compile_initial_snapshots": False,
    }
    for key, val in param_map.items():
        if key in sig.parameters:
            kwargs[key] = val

    prev_env = {
        k: os.environ.get(k)
        for k in (
            "CONTEXT_LAKE_DB_PATH",
            "CONTEXT_LAKE_DATA_DIR",
            "CONTEXT_LAKE_SNAPSHOT_DIR",
            "CONTEXT_LAKE_VAULT_DIR",
            "CONTEXT_LAKE_UPLOAD_DIR",
        )
    }
    os.environ["CONTEXT_LAKE_DB_PATH"] = str(db_path)
    os.environ["CONTEXT_LAKE_DATA_DIR"] = str(tmp_path)
    os.environ["CONTEXT_LAKE_SNAPSHOT_DIR"] = str(snap_dir)
    os.environ["CONTEXT_LAKE_VAULT_DIR"] = str(okf_dir)
    os.environ["CONTEXT_LAKE_UPLOAD_DIR"] = str(upload_dir)

    app = create_app(**kwargs)
    ready = asyncio.Event()
    stop = asyncio.Event()
    startup_err: list[BaseException] = []

    async def _run_lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                ready.set()
                await stop.wait()
        except BaseException as exc:
            startup_err.append(exc)
            ready.set()

    lifespan_task = asyncio.create_task(_run_lifespan())
    await ready.wait()
    if startup_err:
        await lifespan_task
        raise startup_err[0]
    try:
        yield app
    finally:
        stop.set()
        await lifespan_task
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
async def api_client(api_app):
    """Async HTTPX client bound directly to the live FastAPI ASGI app."""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
