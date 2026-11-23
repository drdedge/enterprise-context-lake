"""Runtime AdapterRegistry / ProviderRegistry enabling zero-downtime hexagonal adapter swaps (ADR-002)."""

from __future__ import annotations

from typing import Any, Callable, Literal

from context_core.exceptions import ValidationError
from context_core.graph import GraphProjectionService
from context_core.policy import PolicyEngine
from context_core.ports import RetrievalPort, SnapshotPort, StorePort

StoreName = Literal["sqlite_bitemporal", "okf_markdown_vault"]
RetrievalName = Literal["lexical_manifest", "facet_graph"]
LlmMode = Literal["auto", "gemini", "deterministic"]


class AdapterRegistry:
    """Holds active StorePort, RetrievalPort, SnapshotPort, PolicyEngine, and LLM mode.

    Supports live runtime hot-swapping between coequal Store and Retrieval adapters
    while synchronizing full domain state across stores so mutations are never lost.
    """

    def __init__(
        self,
        *,
        stores: dict[str, StorePort],
        retrieval_factories: dict[str, Callable[[StorePort, PolicyEngine], RetrievalPort]],
        policy: PolicyEngine | None = None,
        snapshot_port: SnapshotPort | None = None,
        active_store: str = "sqlite_bitemporal",
        active_retrieval: str = "lexical_manifest",
        llm_mode: LlmMode = "auto",
    ) -> None:
        if active_store not in stores:
            raise ValidationError(f"Unknown active_store: {active_store}")
        if active_retrieval not in retrieval_factories:
            raise ValidationError(f"Unknown active_retrieval: {active_retrieval}")
        self.policy = policy or PolicyEngine()
        self._stores = dict(stores)
        self._retrieval_factories = dict(retrieval_factories)
        self._active_store_name = active_store
        self._active_retrieval_name = active_retrieval
        self._llm_mode: LlmMode = llm_mode
        self._snapshot_port = snapshot_port
        self._retrieval: RetrievalPort = self._retrieval_factories[active_retrieval](
            self.store, self.policy
        )
        self._graph_service = GraphProjectionService(self.store, self.policy)

    @property
    def active_store_name(self) -> str:
        return self._active_store_name

    @property
    def active_retrieval_name(self) -> str:
        return self._active_retrieval_name

    @property
    def llm_mode(self) -> LlmMode:
        return self._llm_mode

    @property
    def store(self) -> StorePort:
        return self._stores[self._active_store_name]

    @property
    def retrieval(self) -> RetrievalPort:
        return self._retrieval

    @property
    def graph(self) -> GraphProjectionService:
        return self._graph_service

    @property
    def snapshots(self) -> SnapshotPort | None:
        return self._snapshot_port

    def set_snapshot_port(self, snapshot_port: SnapshotPort) -> None:
        self._snapshot_port = snapshot_port

    def get_store(self, name: str) -> StorePort:
        if name not in self._stores:
            raise ValidationError(f"Unknown store adapter: {name}")
        return self._stores[name]

    def describe(self) -> dict[str, Any]:
        return {
            "store": self._active_store_name,
            "retrieval": self._active_retrieval_name,
            "llm_mode": self._llm_mode,
            "available": {
                "store": sorted(self._stores.keys()),
                "retrieval": sorted(self._retrieval_factories.keys()),
                "llm_mode": ["auto", "gemini", "deterministic"],
            },
        }

    async def swap(
        self,
        *,
        store: str | None = None,
        retrieval: str | None = None,
        llm_mode: LlmMode | None = None,
        sync_state: bool = True,
    ) -> dict[str, Any]:
        """Hot-swap active Store, Retrieval, and/or LLM mode without restarting the process."""
        synced = False
        if store is not None and store != self._active_store_name:
            if store not in self._stores:
                raise ValidationError(f"Unknown store adapter: {store}")
            old_store = self.store
            new_store = self._stores[store]
            await new_store.initialize()
            if sync_state and hasattr(old_store, "export_state") and hasattr(new_store, "import_state"):
                state = await old_store.export_state()  # type: ignore[attr-defined]
                await new_store.import_state(state)  # type: ignore[attr-defined]
                synced = True
            self._active_store_name = store
            self._graph_service = GraphProjectionService(self.store, self.policy)
            if self._snapshot_port is not None and hasattr(self._snapshot_port, "set_store"):
                self._snapshot_port.set_store(self.store)  # type: ignore[attr-defined]
            # Rebuild active retrieval bound to new store
            self._retrieval = self._retrieval_factories[self._active_retrieval_name](
                self.store, self.policy
            )

        if retrieval is not None:
            if retrieval not in self._retrieval_factories:
                raise ValidationError(f"Unknown retrieval adapter: {retrieval}")
            self._active_retrieval_name = retrieval
            self._retrieval = self._retrieval_factories[retrieval](self.store, self.policy)

        if llm_mode is not None:
            if llm_mode not in ("auto", "gemini", "deterministic"):
                raise ValidationError(f"Invalid llm_mode: {llm_mode}")
            self._llm_mode = llm_mode

        epochs = await self.store.get_epochs()
        info = self.describe()
        info["synced"] = synced
        info["data_epoch"] = epochs.data_epoch
        info["policy_epoch"] = epochs.policy_epoch
        return info

    async def close_all(self) -> None:
        for s in self._stores.values():
            await s.close()


# Ergonomic alias matching brief naming
ProviderRegistry = AdapterRegistry
