"""Coequal storage, retrieval, and policy adapters for Enterprise Context Lake."""

from __future__ import annotations

from pathlib import Path

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
) -> AdapterRegistry:
    """Create a ready-to-use AdapterRegistry wiring both StorePort and both RetrievalPort adapters.

    Both stores are wrapped in a VersionedStore sharing one version ledger (ADR-007), so every write
    is recorded in history whichever store is active. The change-set service is exposed as
    ``registry.versions`` and the ledger as ``registry.ledger``.
    """
    from context_core.change_service import ChangeSetService

    pol = policy or PolicyEngine()
    ledger = SqliteVersionLedger(ledger_path or ledger_path_for(sqlite_path))
    sqlite_store = VersionedStore(
        SqliteBitemporalStore(db_path=sqlite_path, policy=pol, repo_root=repo_root), ledger
    )
    okf_store = VersionedStore(
        OkfMarkdownVaultStore(vault_dir=okf_vault_dir, policy=pol, repo_root=repo_root), ledger
    )
    registry = AdapterRegistry(
        stores={
            "sqlite_bitemporal": sqlite_store,
            "okf_markdown_vault": okf_store,
        },
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
    for vs in (sqlite_store, okf_store):
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
]
