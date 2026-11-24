# ADR-008: Editable Taxonomy and Access Checks in the Query

- **Status:** Proposed (prototype on SQLite and the Markdown vault). SQL Server and S3 adapters come later.
- **Scope:** `context_core/vocabulary.py`, `context_core/models.py`, `context_adapters/_vocab_common.py`, `context_adapters/sqlite_store.py`, `context_adapters/okf_store.py`, `context_adapters/versioned_store.py`, `context_core/change_service.py`, `context_core/approval_rules.yaml`, `context_api/vocabulary_routes.py`
- **Design review:** "Taxonomy, data model and storage review" (Claude Doc, 24 Nov 2026)

## Context

The categories that describe context were fixed in code. Entity types, identifier schemes, lineage event types, coverage roles, units and similar values were Python enums, and predicates were free text with no register. Adding a new kind of thing meant a code change and a release, and anything unexpected in an upload or an agent write was either rejected or silently stored as an unknown string.

Access checks ran in Python after rows were loaded. That is correct but it does not scale: a page of 50 entities could mean reading every row and dropping most of them, and a SQL Server deployment shared with other users needs the filter in the query itself.

## Decision

### Vocabularies are data

Every category list is a vocabulary of `VocabTerm` rows (`term_id = "<vocab>:<key>"`). A term has a label, an optional parent, aliases, a status (`active`, `proposed`, `deprecated`), an optional `merged_into_key`, free-form attributes and an `owner_group` who stewards it.

| Vocabulary | Used by |
|---|---|
| `entity_type` | `Entity.entity_type` |
| `identifier_scheme` | `EntityIdentifier.scheme` |
| `predicate` | `Claim.predicate` (attribute `object`: `entity` or `literal`) |
| `lineage_event_type`, `position_type`, `coverage_role`, `coverage_status` | the matching records |
| `unit`, `accounting_basis`, `epistemic_temporal`, `actual_stage`, `periodicity`, `ingestion_channel` | claim facets |

Entity types form a tree under a single root, `thing`: `party` (with `organisation` and `person`), `product`, `instrument`, `agreement`, `event`, `place`, `document`, `system`, `initiative` and `concept`. The 23 existing types sit under these, so `issuer` is an `organisation`, which is a `party`. A filter on a broad type (for example `party`) expands to every narrower type, in the graph and in entity listings.

The model fields that held enums now hold normalised keys (`VocabKey`). The old `EntityType` enum stays as the list of seeded values, so existing code and fixtures keep working.

### Unknown values are accepted as proposed

When a write uses a key no vocabulary knows, the store registers it as a `proposed` term (an unknown entity type is parented under `thing`) and the write succeeds. Proposed terms show up at `GET /api/v1/vocabularies/proposed` for a steward to approve, re-parent, merge into an existing term or deprecate. Nothing is lost and nothing waits.

### Taxonomy edits go through change sets

Every edit to a term, including approving a proposed one, is a `vocab_term` item in an ADR-007 change set. The new `vocabulary` rule in `approval_rules.yaml` sends a non-steward's edit to review by a steward of that vocabulary; a steward's own edit is approved automatically by `steward-self`, and an agent or harness edit needs a steward under `agent-privileged`. Automatic registrations of proposed terms are recorded in the version ledger, so history shows where each term first came from. Reverting the creation of a term deprecates it rather than deleting it, because data may still use it. Edits that would point at an unknown parent or merge target, or create a loop, are rejected.

### Access checks in the query (SQLite)

Each distinct `(wall_side, read groups)` combination is stored once in `acl_sets` / `acl_set_groups`, and each entity and claim row carries its `acl_set_id`. Reads join against the caller's readable sets:

```sql
acl_set_id IN (
  SELECT s.acl_set_id FROM acl_sets s JOIN acl_set_groups g USING (acl_set_id)
  WHERE s.wall_side IN (:walls_the_caller_may_see) AND g.group_name IN (:caller_groups)
)
```

Paged entity listings and filtered claim queries now filter, order and limit in SQL. Personal-scope claims (user and user-by-skill memory) are filtered in the same query. The PolicyEngine still checks every row that comes back, so the SQL filter can only narrow what the caller sees, never widen it. Existing databases get the column added and back-filled on start-up.

Because the number of distinct ACL sets is small (tens, not thousands), the subquery stays cheap and maps directly onto a SQL Server table with an index on `acl_set_id`.

## Consequences

- New kinds of context can be folded in by stewards without a release.
- Data arriving from uploads and agents is never rejected for using a new category.
- Stewards get a queue of proposed terms and a history of every change.
- The Markdown vault store still checks access in Python; its scale does not need more.
- Entity page templates (ADR-009) follow the same tree: a template for `product` applies to `legal_vehicle` unless a more specific template exists. `WikiService` reads the parent lookup from the store's vocabulary unless one is passed in.
- The SQL Server adapter can reuse the same tables and the same subquery.

## Follow-ups

- SQL Server adapter (tables in the lake's own schema; same `acl_sets` design).
- A taxonomy screen in the UI (tree, proposed queue, merge).
