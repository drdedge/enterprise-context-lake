# ADR-008: LangSmith Context Hub Export, Import and Migration

- **Status:** Accepted (2026-11-24)
- **Builds on:** ADR-003 (MNPI walls), ADR-007 (version control: releases, channels, change sets)
- **Scope:** `context_core/context_hub.py`, `context_core/context_hub_service.py`, `context_adapters/context_hub_clients.py`, `context_api/context_hub_routes.py`, the `context-hub-import` rule in `approval_rules.yaml`

## Context

The current build is a stopgap, and LangSmith is the likely target platform. Dave wants the lake to be shaped like LangSmith Context Hub, so its context can be ported there.

Hosted LangSmith controls access per workspace and repo only. That is too coarse for material-non-public information. Dave decided that MNPI and other private-side material never goes into LangSmith. The lake keeps its fine-grained access controls. The port covers everything else, in both directions.

Context Hub (LangSmith docs as of 24 Nov 2026) has these properties:

- **Repos:** each repo lives in a workspace. An agent repo holds `AGENTS.md`. A skill repo holds `SKILL.md` and its supporting files.
- **Files:** UTF-8 text only.
- **Commits:** immutable, and a push names its `parent_commit`.
- **Tags:** `staging` and `production` are reserved for promotion.
- **Links:** an agent repo can link to a skill repo with a skill entry.
- **Write API:** the SDK push and pull functions (`push_agent`, `push_skill`, `pull_agent`, `pull_skill`) are the only documented way to write.
- **Not documented:** per-file ACLs, commit authors, and any approval flow.

## Decision

### Export (and the one-off migration)

A release manager exports a named release. Exporting the first release is the migration.

1. **Build the plan.** It is built from the ledger versions at the release's cut time, so it is reproducible.
2. **Only explicitly public-side records go out.** A record is exported only when it is a knowledge item, an entity or a live claim, its ledger version says `public_side`, it has a non-empty `read_acl_groups`, and its payload agrees with both. Everything else is refused, with a reason in the plan:
    - private side or above the wall
    - personal (memory, watchlist)
    - topics
    - an unknown or mismatched wall side or reader list
    - superseded
    - a subject that was not exported
3. **One agent repo per reader set.** For example, `context-lake-firm-all` and `context-lake-pm-credit`. Context Hub can't enforce finer access inside a repo.
4. **Repo layout:**
    - `AGENTS.md`: an index of the repo.
    - `.lake/repo.json`: the manifest, recording wall side and readers.
    - `knowledge/<kind>/<slug>.md`: domain knowledge and data-access guidance.
    - `entities/<type>/<key>.md`: export-only pages listing each entity's claims.
    - `skills/<slug>`: links to skill repos.
5. **Skill procedures become skill repos** named `context-lake-skill-<slug>`. Their `SKILL.md` keeps `name` and `description` in its frontmatter, plus the other metadata, and they are pinned from the agent repo by commit.
6. **All lake metadata lives in a `lake:` frontmatter block.** That covers record id, version, wall side, readers, release and content SHA-256. Context Hub only keeps file text.
7. **Claims never carry evidence excerpts or span locators.** An evidence link is included only when the evidence has no separate ACL. Otherwise the page says the evidence is held in the lake.
8. **Each push names, as its parent, the commit the lake last pushed to that repo.** If the repo's head has moved because someone edited it in Context Hub, the push is refused with `409`. The export is unblocked in one of two ways:
    - The edits are imported, and a steward approves or rejects the resulting change set.
    - The release manager passes `accept_hub_heads`.
9. **The commits are recorded on the release.** `Release.hub_commit` holds a JSON map of repo to commit. A failed export records what landed, so a retry resumes. Exporting a release older than one already exported is refused.
10. **Files that disappeared since the last export are deleted in the next commit.**

### Import

A release manager imports a repo, by default at its latest commit or else at a given tag or commit.

- The repo's readers come from the lake manifest (or the `lake:` block of `SKILL.md`). A repo the lake didn't export needs readers passed explicitly.
- Only `knowledge/<kind>/*.md` files and `SKILL.md` are imported, and only when their content differs from the lake's.
- Each one becomes a knowledge-item upsert that is forced to `public_side` with the repo's readers.
- An item is skipped if it targets a record that isn't public side or has different readers. Entity pages are export-only and are skipped.
- The upserts are filed as one change set with kind `import`, source `context_hub`, and the Context Hub commit as its `run_id`.
- The `context-hub-import` rule always sends it to a steward, and the importer can't approve their own import.

### Backends

- **`FolderHubClient` (default).** Emulates Context Hub's commit model on disk, so the whole flow can be reviewed and tested offline.
- **`LangSmithHubClient` (`CONTEXT_HUB_BACKEND=langsmith`).** Uses the LangSmith SDK, pushes repos as private, and checks the head before every push.

## Consequences

- LangSmith sees public-side context only. Agents hosted there can't answer from private-side context.
- Per-item authorship and approval history stay in the lake. Context Hub shows only the lake's release commits.
- Entity and claim pages are a one-way copy. Bitemporal history and conflict sets don't port.

## Not covered yet

- **Promoting tags.** The LangSmith SDK has no public call to move the `staging` or `production` tags, so promotion in Context Hub is a manual step after export. The folder client supports tags, for tests.
- **Personal memory.** Context Hub's per-user memory repos are keyed to the signed-in person, and there is no documented way to write into them on someone's behalf.
- **Limits.** Repo-size and file-count limits are undocumented. Run a trial export into a test workspace before the real migration.
