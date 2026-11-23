---
name: cashflow-wire-reconciliation
description: Map treasury wires, capital calls, and administrator codes to exact legal vehicles and halt automated routing when an open N-way ConflictSet exists.
scope: skill
wall_side: public_side
read_acl_groups: ["firm-all"]
topics: ["cashflows-liquidity", "operational-admin", "legal-side-letters"]
version: "1.3.0"
metadata:
  include_tools:
    - lookup
    - read
    - resolve_context
    - flag_knowledge_gap
---

# Cash-Flow Wire Reconciliation Skill Procedure

## Purpose & Binding Scope

Treasury cash-flow workflows (`app:treasury-cashflow-engine`) require exact **`legal_vehicle`** granularity (`context_purpose: "legal_vehicle"`, e.g., `veh:alpha-credit-offshore-lp`) and official `has_admin_code` bindings.

## Conflict & Partial-Evidence Safety Protocol

1. Query `resolve_context(entity_keys=["veh:alpha-credit-offshore-lp"], skill_id="skill:cashflow-wire-reconciliation")`.
2. Inspect `conflicts`: if an open `ConflictSet` exists on `has_admin_code` (e.g., 3-way conflict between `ADM-904`, `ADM-904X`, and `ADM-904-GIPS`), **keep deterministic execution pinned to the active `approved_fact` (`ADM-904`)**, surface all competing assertions, and flag for Fund Steward resolution. NEVER guess a winner.
3. If supporting evidence (such as an LP Side Letter PDF) returns `evidence_redacted: true`, do not attempt to infer or extrapolate hidden contract terms; escalate to `fund-stewards` / `custodian-legal-docs`.
4. After steward resolution commits a new `approved_fact` and publishes snapshot `v{N+1}`, adopt the updated `has_admin_code` automatically on the next run with zero skill code edits.
