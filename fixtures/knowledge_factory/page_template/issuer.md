---
okf_version: "0.2"
id: "kf:page-template-issuer"
knowledge_kind: page_template
display_name: "Issuer page"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards"]
valid_from: "2026-11-24T00:00:00Z"
recorded_at: "2026-11-24T00:00:00Z"
---

Page template for issuers. Financial claims get their own section ahead of other facts.

```yaml
entity_types: [issuer, ultimate_parent]
sections:
  - {id: overview, title: Overview, kind: overview}
  - {id: financials, title: Financials, kind: facts, domain_nature: financial}
  - {id: other-facts, title: Other facts, kind: facts, domain_nature: non_financial}
  - {id: conflicts, title: Open conflicts, kind: conflicts}
  - {id: relationships, title: Relationships, kind: related}
  - {id: diagram, title: Relationship diagram, kind: diagram}
  - {id: recent-changes, title: Recent changes, kind: recent_changes, limit: 15}
```
