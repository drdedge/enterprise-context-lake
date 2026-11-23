---
name: crm-fund-mapping
description: Map institutional LP inquiries, commitments, and prospective fundraising opportunities to canonical Fund Families without confusing legal vehicles or GIPS composites.
scope: skill
wall_side: public_side
read_acl_groups: ["firm-all"]
topics: ["client-commercial", "operational-admin"]
version: "1.2.0"
metadata:
  include_tools:
    - lookup
    - read
    - resolve_context
    - propose_update
---

# CRM Fund Mapping Skill Procedure

## Purpose & Binding Scope

CRM workflows (`app:crm-salesforce`) resolve funds strictly at the **`fund_family`** granularity (`context_purpose: "fund_family"`). Never substitute a GIPS reporting composite (`consolidated_group`) or Cayman legal vehicle (`legal_vehicle`) into Salesforce opportunity records.

## Step-by-Step Procedure

1. Call `lookup(canonical_key="skill:crm-fund-mapping", predicate="maps_as", context_purpose="fund_family")` (or read directly from local compiled `v{N}.sqlite.zst` snapshot).
2. Verify whether the client is an existing LP (`commitment`) or prospect (`opportunity`) and check `acts_as_lp_client` / `acts_as_issuer` / `acts_as_counterparty` on canonical `legal_party` records to surface dual-role relationships before LP meetings.
3. If a legacy alias (e.g., `Meridian Opportunistic Credit Offshore LP`) appears in an inbound email, resolve via bitemporal `entity_identifiers` and `renamed_from` lineage to `fam:alpha-credit-family`.
4. If an unknown alias or unmapped consultant mandate is discovered, call `propose_update(kind="gap", ...)` — never overwrite `approved_fact` entries directly.
