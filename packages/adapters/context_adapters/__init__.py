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
) -> AdapterRegistry:
    """Create a ready-to-use AdapterRegistry wiring both StorePort and both RetrievalPort adapters."""
    pol = policy or PolicyEngine()
    sqlite_store = SqliteBitemporalStore(db_path=sqlite_path, policy=pol, repo_root=repo_root)
    okf_store = OkfMarkdownVaultStore(vault_dir=okf_vault_dir, policy=pol, repo_root=repo_root)
    return AdapterRegistry(
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
]
