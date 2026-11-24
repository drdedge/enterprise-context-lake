"""Coequal storage, retrieval, and policy adapters for Enterprise Context Lake."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from context_core.policy import PolicyEngine
from context_core.registry import AdapterRegistry, LlmMode
from context_adapters.okf_store import OkfMarkdownVaultStore
from context_adapters.policy import (
    EntraAuthVerifier,
    MockDirectoryGroupResolver,
    PrincipalDirectory,
    make_local_test_jwt,
    sanitize_uri_secrets,
    validate_no_ssrf,
    wall_allows,
)
from context_adapters.retrieval import FacetGraphRetriever, LexicalManifestRetriever
from context_adapters.sqlite_store import SqliteBitemporalStore
from context_adapters.version_ledger import SqliteVersionLedger, ledger_path_for
from context_adapters.versioned_store import VersionedStore
from context_adapters.sealed import KmsEnvelopeCipher, LocalKeyCipher, SealedText
from context_adapters.sqlserver_store import SqlServerBitemporalStore, SqlServerSettings
from context_adapters.sqlserver_ledger import SqlServerVersionLedger

__version__ = "0.1.0"


def create_default_registry(
    *,
    sqlite_path: str | Path = ":memory:",
    okf_vault_dir: str | Path,
    repo_root: Path | None = None,
    active_store: str = "sqlite_bitemporal",
    active_retrieval: str = "lexical_manifest",
    llm_mode: LlmMode = "auto",
    policy: PolicyEngine | None = None,
    ledger_path: str | Path | None = None,
    sqlserver: "SqlServerSettings | None | bool" = None,
) -> AdapterRegistry:
    """Create a ready-to-use AdapterRegistry wiring both StorePort and both RetrievalPort adapters.

    Both stores are wrapped in a VersionedStore sharing one version ledger (ADR-007), so every write
    is recorded in history whichever store is active. The change-set service is exposed as
    ``registry.versions`` and the ledger as ``registry.ledger``.

    SQL Server (ADR-010): pass ``sqlserver`` settings, or leave it None and set ``LAKE_SQLSERVER_URL``,
    to add the ``sqlserver_bitemporal`` store. The ledger then lives in SQL Server too, next to the
    records, and every store's writes are recorded there. ``sqlserver=False`` ignores the environment.
    """
    from context_core.change_service import ChangeSetService

    pol = policy or PolicyEngine()
    if sqlserver is None and os.environ.get("LAKE_SQLSERVER_URL", "").strip():
        sqlserver = SqlServerSettings.from_env()
    mssql_inner = None
    if isinstance(sqlserver, SqlServerSettings):
        mssql_inner = SqlServerBitemporalStore(sqlserver, policy=pol, repo_root=repo_root)
        ledger: Any = SqlServerVersionLedger(mssql_inner)
    else:
        ledger = SqliteVersionLedger(ledger_path or ledger_path_for(sqlite_path))
    sqlite_store = VersionedStore(
        SqliteBitemporalStore(db_path=sqlite_path, policy=pol, repo_root=repo_root), ledger
    )
    okf_store = VersionedStore(
        OkfMarkdownVaultStore(vault_dir=okf_vault_dir, policy=pol, repo_root=repo_root), ledger
    )
    stores: dict[str, Any] = {"sqlite_bitemporal": sqlite_store, "okf_markdown_vault": okf_store}
    if mssql_inner is not None:
        stores["sqlserver_bitemporal"] = VersionedStore(mssql_inner, ledger)
    registry = AdapterRegistry(
        stores=stores,
        retrieval_factories={
            "lexical_manifest": lambda s, p: LexicalManifestRetriever(
                s,
                p,
                rg_search_root=getattr(s, "vault_dir", None),
            ),
            "facet_graph": lambda s, p: FacetGraphRetriever(s, p),
        },
        policy=pol,
        active_store=active_store,
        active_retrieval=active_retrieval,
        llm_mode=llm_mode,
    )
    registry.ledger = ledger  # type: ignore[attr-defined]
    registry.versions = ChangeSetService(get_store=lambda: registry.store, ledger=ledger, policy=pol)  # type: ignore[attr-defined]
    # Existing write endpoints go through the approval rules too (auto writes straight through).
    for vs in stores.values():
        vs.set_gate(registry.versions.gate_direct_write)  # type: ignore[attr-defined]
    return registry


__all__ = [
    "__version__",
    "SqliteBitemporalStore",
    "OkfMarkdownVaultStore",
    "LexicalManifestRetriever",
    "FacetGraphRetriever",
    "PolicyEngine",
    "PrincipalDirectory",
    "MockDirectoryGroupResolver",
    "EntraAuthVerifier",
    "make_local_test_jwt",
    "sanitize_uri_secrets",
    "validate_no_ssrf",
    "wall_allows",
    "create_default_registry",
    "SqliteVersionLedger",
    "VersionedStore",
    "SqlServerBitemporalStore",
    "SqlServerSettings",
    "SqlServerVersionLedger",
    "KmsEnvelopeCipher",
    "LocalKeyCipher",
    "SealedText",
]
