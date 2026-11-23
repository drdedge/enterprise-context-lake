"""Shared runtime container and caller-bound contextvars for FastMCP & Code Mode."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from context_core.exceptions import AuthenticationError
from context_core.models import ContextAssemblyReceipt, PolicyEpochState, Principal
from context_core.receipts import build_receipt

PrincipalResolver = Callable[[Mapping[str, str]], Awaitable[Principal]]

current_principal_var: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "context_mcp_current_principal",
    default=None,
)


def make_receipt(
    *,
    principal: Principal,
    data_epoch: int,
    policy_epoch: int,
    tool_or_endpoint: str,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
    snapshot_version: int | None = None,
    claim_ids: list[str] | None = None,
    redacted_evidence_claim_ids: list[str] | None = None,
    conflict_set_ids: list[str] | None = None,
    memory_ids: list[str] | None = None,
) -> ContextAssemblyReceipt:
    """Construct an immutable SHA-256-sealed ContextAssemblyReceipt via context_core.receipts."""
    return build_receipt(
        principal=principal,
        epochs=PolicyEpochState(data_epoch=data_epoch, policy_epoch=policy_epoch),
        as_of=as_of,
        recorded_as_of=recorded_as_of,
        claim_ids=claim_ids or [],
        redacted_evidence_claim_ids=redacted_evidence_claim_ids or [],
        conflict_set_ids=conflict_set_ids or [],
        memory_ids=memory_ids or [],
        snapshot_version=snapshot_version,
        tool_or_endpoint=tool_or_endpoint,
    )


@dataclass
class LakeRuntime:
    """Adapter-backed runtime facade injected into FastMCP servers and Code Mode RPC."""

    get_store: Callable[[], Any]
    get_retrieval: Callable[[], Any]
    get_policy: Callable[[], Any]
    get_graph: Callable[[], Any]
    get_snapshots: Callable[[], Any] | None = None
    principal_resolver: PrincipalResolver | None = None
    default_principal: Principal | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def store(self) -> Any:
        return self.get_store()

    @property
    def retrieval(self) -> Any:
        return self.get_retrieval()

    @property
    def policy(self) -> Any:
        return self.get_policy()

    @property
    def graph(self) -> Any:
        return self.get_graph()

    @property
    def snapshots(self) -> Any:
        return self.get_snapshots() if self.get_snapshots is not None else None

    async def resolve_principal(self, headers: Mapping[str, str] | None = None) -> Principal:
        """Resolve and validate the active caller Principal from ContextVar, HTTP headers, or default."""
        bound = current_principal_var.get()
        if bound is not None:
            if bound.revoked or await self.store.is_principal_revoked(bound.user_id):
                raise AuthenticationError("Principal has been revoked")
            return bound
        if self.principal_resolver is not None and headers is not None:
            principal = await self.principal_resolver(headers)
            if principal.revoked or await self.store.is_principal_revoked(principal.user_id):
                raise AuthenticationError("Principal has been revoked")
            return principal
        if self.default_principal is not None:
            if self.default_principal.revoked or await self.store.is_principal_revoked(
                self.default_principal.user_id
            ):
                raise AuthenticationError("Principal has been revoked")
            return self.default_principal
        raise AuthenticationError("Missing authenticated principal")
