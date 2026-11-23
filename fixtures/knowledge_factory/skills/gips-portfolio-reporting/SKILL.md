---
name: gips-portfolio-reporting
description: Resolve funds to GIPS consolidated reporting composites with strict bitemporal as_of and recorded_as_of historical replay and mandatory disclosure rules.
scope: skill
wall_side: public_side
read_acl_groups: ["firm-all"]
topics: ["performance-returns", "nav-valuation", "operational-admin"]
version: "1.4.0"
metadata:
  include_tools:
    - lookup
    - filter
    - read
    - resolve_context
    - neighbours
---

# GIPS Portfolio Reporting Skill Procedure

## Purpose & Binding Scope

Portfolio and investor reporting (`app:portfolio-reporting-suite`) resolves funds strictly at the **`consolidated_group`** granularity (`context_purpose: "consolidated_group"` -> e.g., `grp:alpha-credit-gips-composite`).

## Mandatory Compliance Rules (`is_mandatory: true`)

- Every generated GIPS performance table MUST state the `actual_stage` (`prelim`, `unaudited`, `audited`, or `restated`) alongside `snapshot_version` and `receipt_id`.
- Personal formatting preferences (`user_x_skill`) may adjust summary layout or bullet ordering, but CANNOT override mandatory GIPS disclosure clauses or institutional NAV/IRR facts.

## Bitemporal Historical Reproduction Procedure

1. Pass both `as_of` (business period end, e.g., `2025-12-31T23:59:59Z`) and `recorded_as_of` (publication timestamp) — or pin `snapshot_version` — when reproducing any historical investor letter.
2. Pre-restatement replay (`recorded_as_of=2026-01-20T00:00:00Z`) MUST yield `composite_nav_usd = 1,284,500,000` (`prelim`) and `composite_net_irr_pct = 11.42%`.
3. Post-restatement replay (`recorded_as_of=2026-02-25T00:00:00Z`) MUST yield `composite_nav_usd = 1,281,900,000` (`restated`) and `composite_net_irr_pct = 11.28%`.
