---
okf_version: "0.2"
id: "kf:domain-alpha-lme-playbook"
knowledge_kind: domain_knowledge
display_name: "Drop-Down Liability Management Exercise (LME) Credit Playbook"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards", "investment-team"]
topics: ["lme-restructuring", "leverage-covenants", "investment-thesis"]
related_entities: ["issuer:apex-industrial", "issuer:apex-newco-operating", "sponsor:blackridge-capital-partners"]
valid_from: "2026-02-14T00:00:00Z"
recorded_at: "2026-02-14T16:30:00Z"
---

# Drop-Down Liability Management Exercise (LME) Credit Playbook

## Institutional Rule

When a PE-sponsored borrower transfers material intellectual property or operating assets into an unrestricted subsidiary (a J.Crew / drop-down LME), analysts and reporting workflows must preserve **both** the legacy borrower (`issuer:apex-industrial`) and the unrestricted subsidiary (`issuer:apex-newco-operating`) linked via an explicit `lme_restructured_to` lineage event rather than overwriting the original entity record.

## Key Evaluation Checklist

1. **Investment Basket Leakage:** Quantify unrestricted subsidiary transfer capacity across General Investments, Available Amount / Builder Basket, and Permitted Acquisitions.
2. **Co-Op & Blocker Clauses:** Verify presence of J.Crew, Serta, Chewy, and Pluralsight blockers in the governing credit agreement before sizing new commitments (reason `issuer:redwood-retail` was formally passed on 2026-01-22).
3. **Bitemporal Valuation Impact:** Any retroactive fair-value markdown triggered by an LME (e.g., Q4 2025 Apex Industrial 1L TLB -$2.6M audit adjustment) must be recorded as a `restated` claim superseding the `prelim` claim while preserving full `recorded_at` / `superseded_at` replay fidelity.
4. **Information Barrier Separation:** Public-side research notes may reference announced LME filings only; NDA-governed ad-hoc lender exchange proposals (e.g., `proj:project-cobalt-lme`) remain strictly segregated on `private_side`.
