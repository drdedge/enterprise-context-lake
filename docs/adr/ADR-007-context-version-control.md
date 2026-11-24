# ADR-007: Git-like Version Control for Context

- **Status:** Proposed (prototype). The approval rules are a draft awaiting review.
- **Scope:** `context_core/versioning.py`, `context_core/change_service.py`, `context_core/approval_rules.yaml`, `context_adapters/versioned_store.py`, `context_adapters/version_ledger.py`, `context_api/versioning_routes.py`, `apps/web/src/components/HistoryPanel.tsx`
- **Design review:** "Version control for the Context Lake" (Claude Doc, 24 Nov 2026)

## Context

Claims were already append-only and bitemporal, but entities, knowledge notes, topics, personal memories and watchlists were overwritten in place, and nothing recorded who changed what or why. The owner asked for git-like version control:

- Changes are proposed and approved, with some approved automatically depending on the kind of change or who makes it.
- Changes are shown as redlines.
- Changes can be rolled back.
- Clear versions exist, so evals can run against a `dev` version of context before it is published.
- Each piece of context has a history view in the style of a VS Code git graph.

Context reaches the lake in four ways: uploads, harnesses using the MCP server or API, the lake's own agents, and direct edits in the UI. All four go through the lake, so history lives in the lake, not in a separate git repository.

## Decision

| Git idea | Lake concept |
|---|---|
| Commit of one file | `VersionRecord`: an immutable version of one record, holding its wall side, read groups and owner |
| Pull request | `ChangeSet`: items with before and after images, author, source (`ui`, `api`, `harness`, `agent`, `upload`), agent id and run id |
| Branch protection | `approval_rules.yaml`: first matching rule per item; the change set takes the strictest outcome |
| Merge | Applied through the normal store methods, so all existing policy checks still run |
| `git revert` | A new change set with the opposite edits, which goes through the same rules |
| Tag | `Release`: a numbered system-time cut of the whole lake |
| Moving tag | `dev`, `staging` and `production` channels pointing at releases |

### History capture

`VersionedStore` wraps every StorePort adapter. After each successful write it appends the record's new state to the version ledger.

- **Writes made during a merge** are filed under that change set.
- **Every other write** gets its own implicit change set that is already merged. Today that covers the existing REST, MCP and agent paths, which don't go through review yet. Those writes still show up in history with their author and source.
- **Attribution** comes from the request's principal, plus the `X-Change-Source`, `X-Agent-Id` and `X-Run-Id` headers when present.
- **Claims** are keyed by "slot": `(subject, predicate, context_purpose, scope_type, scope_id)`. A slot is the piece of context that successive claims rewrite.

### Existing endpoints go through the rules

The claim and topic endpoints that already existed (`POST /api/v1/claims`, claim updates and supersedes, `POST`/`PATCH /api/v1/topics`, uploads, and the HTTP MCP and agent paths that call the same store methods) are checked against the approval rules before they write:

- **Auto-approved** writes go straight through, as before, and are recorded in history.
- **Writes that need review** are filed as a proposed change set, and the endpoint returns `202` with `{"pending_approval": {...change set...}}`. Over MCP the tool gets a `pending_approval` error naming the change set.
- **Writes the caller may not make at all** are still refused with `403`, not parked as proposals. Use `POST /api/v1/change-sets` to propose an edit you can't make yourself.

### Information barrier

The barrier is enforced in the application layer, on every version, diff, change set and release listing:

- **Two checks per version.** A version is shown only if the viewer can read that version and can also read the record's current version. So a record reclassified from public side to private side disappears, history included, for public-side viewers.
- **Evidence redaction** applies to old claim versions too.
- **Proposals.** Authors can't propose changes to records they can't read.
- **Hidden change sets.** A change set with any item the viewer can't read is hidden in full.

### Releases

A release records a cut timestamp. Reading a channel or release passes that timestamp as `recorded_as_of`. Claims already answer that query bitemporally, and other records come from `version_at(cut)`. No data is copied.

`GET /api/v1/claims` and `GET /api/v1/context/resolve` accept `channel=` and `release=`. Cutting releases and moving channels is limited to `release_managers` in the rules file.

### Storage on SQL Server

The prototype ledger is a separate SQLite file (`<db>.versions.sqlite3`), so it survives a runtime store swap. On SQL Server it becomes plain append-only tables written in the same transaction as the record.

Temporal tables were rejected. The service account has `db_ddladmin` but not `CONTROL` on its tables, so it couldn't switch system versioning off. That would block purging history, for example MNPI uploaded to the wrong side, and also dropping or rebuilding those tables. Row-level security isn't available either (no `ALTER ANY SECURITY POLICY`), which is why barrier filtering stays in the application layer.

### LangSmith Context Hub alignment

- The channel names match Context Hub's tags: `staging` and `production`.
- `Release.hub_commit` stores the Context Hub commit hash once a release is exported.
- Approval, redlines and per-author history stay in the lake, because Context Hub doesn't document them.

## Limitations of the prototype

- **Merges aren't atomic across stores.** A merge applies each item through the store and records versions in the ledger afterwards. If an item fails part-way, the change set is marked `failed`, and the items already applied stay applied and visible in history.
- **Claim slots can hold several live claims** (an open conflict, or different valid-time windows). Slot history is a timeline of events, and revert works on the latest event.
- **Undoing the creation of a note isn't supported.** Notes have no archived status yet. Entities and topics are archived on revert.
- **Some write paths are not gated.** Memory and watchlist edits are personal and always auto-approved. Inbox approve/reject and conflict resolution are already steward decisions, so they write directly. Bulk imports, seeding and the stdio MCP server (no request attribution) are recorded but not gated.
- **Publishing is linear.** Moving `production` publishes everything merged before that release. A bad change is reverted, not skipped.

## API

- **Change sets:** `POST /api/v1/change-sets`, `GET /api/v1/change-sets[?status=&include_direct=]`, `GET /api/v1/change-sets/{id}`, and `POST /api/v1/change-sets/{id}/{approve|reject|revert}`.
- **History:** `GET /api/v1/history/{type}/{id}`, `.../diff?from=&to=`, `.../versions/{n}` and `.../at?channel=|release=`, plus `GET /api/v1/history/claim-slots/{entity_id}`.
- **Releases and channels:** `GET|POST /api/v1/releases`, `GET /api/v1/channels`, `PUT /api/v1/channels/{name}` and `GET /api/v1/channels/{name}/as-of`.
- **Rules:** `GET /api/v1/approval-rules`.
