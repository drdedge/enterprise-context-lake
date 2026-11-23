---
okf_version: "0.2"
id: "kf:dag-live-oms-pms"
knowledge_kind: data_access_guidance
display_name: "Data Access Guidance: Live OMS / PMS Positions & Intraday Pricing"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards", "investment-team"]
topics: ["operational-admin", "nav-valuation"]
related_entities: ["src:oms-pms", "veh:alpha-credit-master-lp", "veh:northstar-dl-ii-lp"]
valid_from: "2025-01-01T00:00:00Z"
recorded_at: "2025-01-10T09:00:00Z"
---

# Data Access Guidance: Live OMS / PMS Positions

## Do Not Duplicate Intraday Positions in the Context Lake

The Enterprise Context Lake stores canonical entity identities, bitemporal identifier crosswalks (`LEI`, `OpenFIGI`, `CUSIP`, `ISIN`, `admin_code`), end-of-day reified position snapshots for impact traversal, and governance rules. **Intraday positions, live bid/ask quotes, and real-time trade blots MUST be queried directly from the live OMS/PMS System of Record (`src:oms-pms`).**

## Live Query Contract

- **System of Record:** Meridian OMS / PMS SQL Read Replica (`oms_warehouse.dbo.v_live_positions`) or internal REST gateway `/internal/oms/v2/positions`.
- **Join Keys (from Context Lake Crosswalk):**
  - Join Legal Vehicle via `scheme="admin_code"` (`ADM-904`, `ADM-903`, `CITCO-NSDL2-01`) or `scheme="lei"`.
  - Join Instrument via `scheme="openfigi"` (`BBG01APEX1LTLB8`, `BBG01GAFG625291`) or `scheme="isin"`.
- **Freshness & Caveats:** Intraday marks lag Bloomberg BVAL by ≤ 60 seconds; end-of-day official valuation strikes at 17:00 ET and reconciles against Northern Trust (`src:admin-feed`) by T+1 06:00 UTC.
