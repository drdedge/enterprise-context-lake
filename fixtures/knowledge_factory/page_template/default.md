---
okf_version: "0.2"
id: "kf:page-template-default"
knowledge_kind: page_template
display_name: "Default entity page"
wall_side: public_side
read_acl_groups: ["firm-all"]
write_acl_groups: ["fund-stewards"]
valid_from: "2026-11-24T00:00:00Z"
recorded_at: "2026-11-24T00:00:00Z"
---

Page template used for every entity type that has no template of its own (`*`).

Pages are read views built from claims. To change what a page says, change the claims behind it.
To change what a page shows, edit this template through a change set. The lake reads the YAML block.

Section kinds: `overview`, `facts`, `directives`, `related`, `diagram`, `conflicts`, `recent_changes`.
Optional filters per section: `predicates`, `exclude_predicates`, `context_purposes`,
`domain_nature` (`financial` or `non_financial`) and `limit`.

```yaml
entity_types: ["*"]
sections:
  - {id: overview, title: Overview, kind: overview}
  - {id: key-facts, title: Key facts, kind: facts}
  - {id: relationships, title: Relationships, kind: related}
  - {id: diagram, title: Relationship diagram, kind: diagram}
  - {id: conflicts, title: Open conflicts, kind: conflicts}
  - {id: directives, title: Directives, kind: directives}
  - {id: recent-changes, title: Recent changes, kind: recent_changes, limit: 10}
```
