---
okf_version: "0.2"
id: "kf:domain-dual-role-party-policy"
knowledge_kind: domain_knowledge
display_name: "FIBO / GLEIF Party-Role Separation Policy for Dual-Role Institutions"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards", "compliance-oversight"]
topics: ["governance-controls", "client-commercial", "operational-admin"]
related_entities: ["party:global-atlantic-bank", "issuer:global-atlantic-bank", "commit:global-atlantic-alpha-offshore-125m"]
valid_from: "2024-03-01T00:00:00Z"
recorded_at: "2024-03-02T12:00:00Z"
---

# FIBO / GLEIF Party-Role Separation Policy

## Canonical Principle

Large financial institutions frequently interact with Meridian Credit & Multi-Strategy Partners across multiple independent commercial capacities simultaneously. Creating duplicate legal-entity records breaks consolidated risk aggregation, cross-trade compliance checks, and LP conflict disclosures.

## Reference Example: Global Atlantic Financial Group PLC

- **Canonical Legal Party:** `party:global-atlantic-bank` (LEI `5493008G7K2M9X1Q4L88`).
- **Role 1 — Corporate Bond Issuer (`acts_as_issuer`):** `issuer:global-atlantic-bank` -> issues `inst:gafg-625-sr-2029` ($22.0M par held in `veh:alpha-credit-master-lp`).
- **Role 2 — Institutional LP Investor (`acts_as_lp_client`):** Holds `$125M` Class A commitment (`commit:global-atlantic-alpha-offshore-125m`) in `veh:alpha-credit-offshore-lp`.
- **Role 3 — Derivative Counterparty (`acts_as_counterparty`):** Executed ISDA 2002 Master Agreement & CSA with `veh:alpha-credit-master-lp`.

## Operational Requirement

Whenever any skill, report, or impact traversal inspects exposure to `party:global-atlantic-bank`, traverse all active role edges from the canonical `legal_party` rather than matching display names across CRM and OMS extracts.
