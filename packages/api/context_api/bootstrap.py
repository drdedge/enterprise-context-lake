"""Runtime wiring for Enterprise Context Lake (AdapterRegistry, Auth, Uploads, Snapshots, FastMCP)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cachetools import TTLCache
from dotenv import load_dotenv

from context_adapters import create_default_registry
from context_core.models import ActorMode, ChannelMode
from context_core.policy import PolicyEngine, PrincipalDirectory
from context_core.registry import AdapterRegistry, LlmMode
from context_core.seeder import seed_store_from_fixtures
from context_core.wiki import WikiService
from context_agents.wiki_narrators import narrators_from_env
from context_mcp.runtime import LakeRuntime

from context_api.auth import AuthService
from context_api.events import EventBus
from context_api.uploads import UploadService

load_dotenv(override=False)

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]


def _try_build_snapshot_compiler(
    store: Any,
    policy: PolicyEngine,
    snapshots_dir: Path,
    *,
    events: EventBus | None = None,
) -> Any | None:
    """Instantiate SnapshotCompiler from context_sdk when available."""
    try:
        from context_sdk import SnapshotCompiler

        snapshots_dir.mkdir(parents=True, exist_ok=True)

        def _on_pub(manifest: Any) -> None:
            if events is not None:
                events.publish(
                    "snapshot_published",
                    {
                        "acl_tier": manifest.acl_tier,
                        "wall_side": manifest.wall_side.value,
                        "version_id": manifest.version_id,
                        "etag": manifest.etag,
                    },
                )

        return SnapshotCompiler(
            store,
            snapshots_dir,
            policy=policy,
            on_published=_on_pub,
        )
    except Exception:
        return None


@dataclass
class AppRuntimeContext:
    """Lifecycle-managed runtime container owned by FastAPI lifespan or stdio MCP."""

    repo_root: Path
    runtime_dir: Path
    registry: AdapterRegistry
    auth: AuthService
    events: EventBus
    uploads: UploadService
    mcp_runtime: LakeRuntime
    wiki: Any = None
    bundle_cache: TTLCache[Any, Any] = field(default_factory=lambda: TTLCache(maxsize=256, ttl=300.0))
    seed_fixtures: bool = True
    compile_initial_snapshots: bool = True
    _initialized: bool = False
    _temp_dir: tempfile.TemporaryDirectory[str] | None = None

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.registry.store.initialize()
        if self.seed_fixtures:
            await seed_store_from_fixtures(self.registry.store, repo_root=self.repo_root)
        if self.compile_initial_snapshots and self.registry.snapshots is not None:
            try:
                await self.registry.snapshots.compile_and_publish("firm-all", "public_side")
            except Exception:
                pass
        self._initialized = True

    async def close(self) -> None:
        self.events.clear()
        self.bundle_cache.clear()
        if self.registry.snapshots is not None and hasattr(self.registry.snapshots, "close"):
            try:
                res = self.registry.snapshots.close()
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass
        await self.registry.close_all()
        ledger = getattr(self.registry, "ledger", None)
        if ledger is not None:
            ledger.close()
        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except Exception:
                pass
            self._temp_dir = None
        self._initialized = False


async def create_runtime_context(
    *,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    fixtures_dir: Path | str | None = None,
    active_store: Literal["sqlite_bitemporal", "okf_markdown_vault"] = "sqlite_bitemporal",
    active_retrieval: Literal["lexical_manifest", "facet_graph"] = "lexical_manifest",
    llm_mode: LlmMode = "auto",
    auth_mode: Literal["mock", "entra_jwt"] | None = None,
    default_user_id: str | None = "user:fund-steward",
    default_actor_mode: ActorMode = "obo",
    default_channel_mode: ChannelMode = "direct",
    seed_fixtures: bool = True,
    compile_initial_snapshots: bool = True,
    ephemeral: bool = False,
) -> AppRuntimeContext:
    """Build and initialize a complete AppRuntimeContext for FastAPI or stdio FastMCP."""
    ctx = build_runtime_context_sync(
        repo_root=repo_root,
        db_path=db_path,
        runtime_dir=runtime_dir,
        fixtures_dir=fixtures_dir,
        active_store=active_store,
        active_retrieval=active_retrieval,
        llm_mode=llm_mode,
        auth_mode=auth_mode,
        default_user_id=default_user_id,
        default_actor_mode=default_actor_mode,
        default_channel_mode=default_channel_mode,
        seed_fixtures=seed_fixtures,
        compile_initial_snapshots=compile_initial_snapshots,
        ephemeral=ephemeral,
    )
    await ctx.initialize()
    return ctx


def build_runtime_context_sync(
    *,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    fixtures_dir: Path | str | None = None,
    active_store: Literal["sqlite_bitemporal", "okf_markdown_vault"] = "sqlite_bitemporal",
    active_retrieval: Literal["lexical_manifest", "facet_graph"] = "lexical_manifest",
    llm_mode: LlmMode = "auto",
    auth_mode: Literal["mock", "entra_jwt"] | None = None,
    default_user_id: str | None = "user:fund-steward",
    default_actor_mode: ActorMode = "obo",
    default_channel_mode: ChannelMode = "direct",
    seed_fixtures: bool = True,
    compile_initial_snapshots: bool = True,
    ephemeral: bool = False,
) -> AppRuntimeContext:
    """Synchronously construct AppRuntimeContext (initialized inside FastAPI lifespan)."""
    root = Path(repo_root).resolve() if repo_root else DEFAULT_REPO_ROOT
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    if runtime_dir is not None:
        rdir = Path(runtime_dir).resolve()
        rdir.mkdir(parents=True, exist_ok=True)
    elif ephemeral:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="context-lake-")
        rdir = Path(temp_dir_obj.name)
    else:
        rdir = root / "data" / "runtime"
        rdir.mkdir(parents=True, exist_ok=True)

    sqlite_target: str | Path = db_path if db_path is not None else ":memory:"
    vault_dir = rdir / "okf_vault"
    uploads_dir = rdir / "uploads"
    snapshots_dir = rdir / "snapshots"

    policy = PolicyEngine()
    registry = create_default_registry(
        sqlite_path=sqlite_target,
        okf_vault_dir=vault_dir,
        repo_root=root,
        active_store=active_store,
        active_retrieval=active_retrieval,
        llm_mode=llm_mode,
        policy=policy,
    )

    events = EventBus()
    compiler = _try_build_snapshot_compiler(registry.store, policy, snapshots_dir, events=events)
    if compiler is not None:
        registry.set_snapshot_port(compiler)

    fdir = Path(fixtures_dir).resolve() if fixtures_dir else (root / "fixtures")
    principals_path = fdir / "principals.json"
    directory = PrincipalDirectory(principals_path if principals_path.is_file() else None)

    auth = AuthService(
        directory=directory,
        auth_mode=auth_mode,
        default_user_id=default_user_id,
        default_actor_mode=default_actor_mode,
        default_channel_mode=default_channel_mode,
    )
    uploads = UploadService(storage_dir=uploads_dir, repo_root=root)

    default_principal = None
    if default_user_id and os.environ.get("ENVIRONMENT", "").lower() != "production":
        try:
            default_principal = directory.resolve_mock_principal(
                default_user_id,
                actor_mode=default_actor_mode,
                channel_mode=default_channel_mode,
            )
        except Exception:
            default_principal = None

    async def _resolve_for_mcp(headers: Any) -> Any:
        return await auth.resolve_principal(headers, store=registry.store)

    wiki = WikiService(
        get_store=lambda: registry.store,
        get_retrieval=lambda: registry.retrieval,
        narrators=narrators_from_env(llm_mode),
        models_enabled=lambda: registry.llm_mode != "deterministic",
    )

    mcp_runtime = LakeRuntime(
        get_store=lambda: registry.store,
        get_retrieval=lambda: registry.retrieval,
        get_policy=lambda: registry.policy,
        get_graph=lambda: registry.graph,
        get_snapshots=lambda: registry.snapshots,
        principal_resolver=_resolve_for_mcp,
        default_principal=default_principal,
        extra={"wiki": wiki, "versions": lambda: getattr(registry, "versions", None)},
    )

    return AppRuntimeContext(
        repo_root=root,
        runtime_dir=rdir,
        registry=registry,
        auth=auth,
        events=events,
        uploads=uploads,
        mcp_runtime=mcp_runtime,
        wiki=wiki,
        seed_fixtures=seed_fixtures,
        compile_initial_snapshots=compile_initial_snapshots,
        _temp_dir=temp_dir_obj,
    )
