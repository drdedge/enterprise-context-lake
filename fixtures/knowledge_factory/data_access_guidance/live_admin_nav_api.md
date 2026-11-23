---
okf_version: "0.2"
id: "kf:dag-live-admin-nav"
knowledge_kind: data_access_guidance
display_name: "Data Access Guidance: Live Administrator NAV & Wire Feed"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards"]
topics: ["operational-admin", "cashflows-liquidity", "nav-valuation"]
related_entities: ["src:admin-feed", "veh:alpha-credit-offshore-lp", "grp:alpha-credit-gips-composite"]
valid_from: "2025-01-01T00:00:00Z"
recorded_at: "2025-01-10T09:15:00Z"
---

# Data Access Guidance: Live Administrator NAV & Wire Feed

## Authoritative System of Record

Official net asset values, share-class capital account balances, and settled cash wire confirmations originate from Northern Trust (`src:admin-feed`) and Citco (`CITCO-NSDL2-01`).

## Live Query Contract

- **NAV Endpoint:** Internal Treasury Gateway `GET /internal/admin/v1/nav?admin_code={code}&as_of={YYYY-MM-DD}`.
- **Always Resolve `admin_code` First:** Query Context Lake `lookup(canonical_key="veh:alpha-credit-offshore-lp", predicate="has_admin_code", context_purpose="legal_vehicle", as_of=...)` before calling the Administrator API so historical pre-rename (`MOC-704`) and post-amendment (`ADM-904` / `ADM-904X`) periods route cleanly.
- **Restatement Check:** Official audited/restated quarterly numbers supersede preliminary estimates; always verify `actual_stage` (`prelim | unaudited | audited | restated`) before distributing LP reports.
