"""Immutable ContextAssemblyReceipt builder and canonical SHA-256 verification (ADR-003 §6)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Sequence

from context_core.compression import canonical_json, ensure_utc, format_utc, utc_now
from context_core.models import (
    Claim,
    ConflictSet,
    ContextAssemblyReceipt,
    MemoryRecord,
    PolicyEpochState,
    Principal,
)
from context_core.tracing import record_receipt


def compute_receipt_sha256(receipt: ContextAssemblyReceipt) -> str:
    """Compute deterministic SHA-256 over the canonical payload of a receipt (excluding receipt_sha256)."""
    payload = {
        "receipt_id": receipt.receipt_id,
        "timestamp": format_utc(receipt.timestamp),
        "principal_id": receipt.principal_id,
        "auth_mode": receipt.auth_mode,
        "actor_mode": receipt.actor_mode,
        "channel_mode": receipt.channel_mode,
        "wall_side": receipt.wall_side.value,
        "snapshot_version": receipt.snapshot_version,
        "data_epoch": receipt.data_epoch,
        "policy_epoch": receipt.policy_epoch,
        "as_of": format_utc(receipt.as_of),
        "recorded_as_of": format_utc(receipt.recorded_as_of),
        "claim_ids": sorted(set(receipt.claim_ids)),
        "redacted_evidence_claim_ids": sorted(set(receipt.redacted_evidence_claim_ids)),
        "conflict_set_ids": sorted(set(receipt.conflict_set_ids)),
        "memory_ids": sorted(set(receipt.memory_ids)),
        "tool_or_endpoint": receipt.tool_or_endpoint,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_receipt(
    *,
    principal: Principal,
    epochs: PolicyEpochState,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
    claims: Sequence[Claim] = (),
    claim_ids: Sequence[str] | None = None,
    redacted_evidence_claim_ids: Sequence[str] | None = None,
    conflicts: Sequence[ConflictSet] = (),
    conflict_set_ids: Sequence[str] | None = None,
    memories: Sequence[MemoryRecord] = (),
    memory_ids: Sequence[str] | None = None,
    snapshot_version: int | None = None,
    tool_or_endpoint: str = "resolve_context",
    receipt_id: str | None = None,
    timestamp: datetime | None = None,
) -> ContextAssemblyReceipt:
    """Construct an immutable, SHA-256-sealed ContextAssemblyReceipt."""
    eff_ts = ensure_utc(timestamp) or utc_now()
    eff_as_of = ensure_utc(as_of) or eff_ts
    eff_rec = ensure_utc(recorded_as_of) or eff_ts

    all_claim_ids = sorted(
        set(claim_ids if claim_ids is not None else [c.claim_id for c in claims])
    )
    redacted_ids = sorted(
        set(
            redacted_evidence_claim_ids
            if redacted_evidence_claim_ids is not None
            else [c.claim_id for c in claims if c.evidence_redacted]
        )
    )
    all_conflict_ids = sorted(
        set(conflict_set_ids if conflict_set_ids is not None else [cs.conflict_set_id for cs in conflicts])
    )
    # Enforce hard invariant: memory_ids MUST be empty when channel_mode=="shared" or actor_mode=="service"
    if principal.channel_mode == "shared" or principal.actor_mode == "service" or principal.revoked:
        all_memory_ids: list[str] = []
    else:
        all_memory_ids = sorted(
            set(memory_ids if memory_ids is not None else [m.memory_id for m in memories])
        )

    rid = receipt_id or f"rcpt-{uuid.uuid4().hex[:16]}"
    receipt = ContextAssemblyReceipt(
        receipt_id=rid,
        timestamp=eff_ts,
        principal_id=principal.user_id,
        auth_mode=principal.auth_mode,
        actor_mode=principal.actor_mode,
        channel_mode=principal.channel_mode,
        wall_side=principal.wall_side,
        snapshot_version=snapshot_version,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        claim_ids=all_claim_ids,
        redacted_evidence_claim_ids=redacted_ids,
        conflict_set_ids=all_conflict_ids,
        memory_ids=all_memory_ids,
        tool_or_endpoint=tool_or_endpoint,
        receipt_sha256="",
    )
    sealed = receipt.model_copy(update={"receipt_sha256": compute_receipt_sha256(receipt)})
    record_receipt(sealed)
    return sealed


def verify_receipt(receipt: ContextAssemblyReceipt) -> bool:
    """Verify tamper-evident integrity of a ContextAssemblyReceipt."""
    return bool(receipt.receipt_sha256) and receipt.receipt_sha256 == compute_receipt_sha256(receipt)
