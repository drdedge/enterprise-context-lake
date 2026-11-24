# ADR-009: Entity Pages Built from Claims (ideas borrowed from DeepWiki)

- **Status:** Proposed (2026-11-24)
- **Builds on:** ADR-001 (claims), ADR-003 (MNPI walls), ADR-004 (MCP), ADR-007 (change sets, releases, channels)
- **Scope:** `context_core/wiki.py`, `context_core/model_boundary.py`, `context_agents/wiki_narrators.py`, `context_api/wiki_routes.py`, `context_mcp/wiki_tools.py`, `apps/web/src/components/WikiPagePanel.tsx`, `fixtures/knowledge_factory/page_template/`, and a guard in `context_agents/viewport_agent.py`

## Context

Dave asked what is worth taking from DeepWiki: Cognition's hosted [deepwiki.com](https://deepwiki.com/) and the open-source [deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open). DeepWiki generates a navigable wiki for a code repository, links statements to source lines, draws diagrams, takes a steering file (`.devin/wiki.json`), and serves it all through three MCP tools: `read_wiki_structure`, `read_wiki_contents` and `ask_wiki_question`.

The lake already agreed to show wiki-style entity pages as views over dated claims. Using DeepWiki itself was ruled out:

- The hosted service sends content to a third party, so MNPI cannot go there.
- deepwiki-open is built around git repositories and has no access control or versioning.

So the ideas are rebuilt on the lake's own model, and no DeepWiki code or dependency is added.

## Decision

| DeepWiki idea | Lake version |
|---|---|
| Generated page per module | A page per entity, assembled on request from the claims the caller may read. Pages are views, never stored records, and are never edited directly. |
| Links to source lines | Every statement carries numbered citations to its claims: source system, and the evidence location only when the caller may see evidence. |
| `.devin/wiki.json` steering file | Page templates per entity type, stored as knowledge items of kind `page_template`. Edits go through change sets with redlines, approval rules and revert. |
| Architecture diagrams | A Mermaid relationship diagram drawn from the same visible claims as the page. |
| `read_wiki_structure` / `read_wiki_contents` / `ask_wiki_question` | MCP tools with the same names, plus REST: `GET /api/v1/wiki/pages`, `GET /api/v1/wiki/pages/{key}` (`format=markdown`), `POST /api/v1/wiki/ask`. |
| Scheduled regeneration | Regenerated as soon as any claim behind a page changes (see Freshness). |

### Information barrier

A page is built for one **side**, exactly like the snapshot partitions in ADR-003:

- A public-side page uses public-side records only.
- A private-side page uses public-side and private-side records.
- The caller's own side is the default. Asking for a side the caller may not see returns `403`, and an entity that does not exist on the requested side returns `404`.
- Personal (`user`, `user_x_skill`) and `run` scoped claims never appear on pages.
- An open conflict is shown only if every claim in it is visible on that side. Otherwise its existence would hint at hidden material.
- Related entities, diagram nodes and template choice are all filtered by side as well.

### Model boundary

The overview paragraph and answers can be written by a model. `model_boundary.py` classifies each model as `internal` (runs inside the firm's boundary) or `external` (a third-party API such as Gemini):

- `NarratorConfig` refuses an external model for the private side at construction time.
- `WikiService` checks again at call time: an external model is called only when every input (the side, the entity and every claim) is public-side. Anything unknown fails closed.
- Model output must cite only the numbered statements it was given, in every paragraph or bullet. Otherwise it is discarded and the deterministic writer is used.
- The same guard now protects the existing viewport chat agent. Before this change it could send private-side claims to Gemini when a key was configured.

Configuration: `CONTEXT_WIKI_NARRATOR` (`deterministic`, `gemini` or `internal`) for public-side pages, `CONTEXT_WIKI_PRIVATE_NARRATOR` (`deterministic` or `internal`) for the rest, and `CONTEXT_WIKI_INTERNAL_MODEL_URL` / `_MODEL` / `_KEY` for an OpenAI-compatible endpoint the firm runs itself. The default is deterministic everywhere, with no network calls.

### Freshness and versions

Narration, the only costly part, is cached by a fingerprint of everything that went into it: side, template version, entity, and each claim's id, timestamps, value and evidence-redaction flag. Any claim change produces a new fingerprint, so a page is never served stale. Pages accept `as_of`, `recorded_as_of`, `channel` and `release`, so evals can read a page as it stood in `production` while `dev` moves on. Every page read and answer records a context assembly receipt.

### Templates and the taxonomy

The template for an entity type is chosen in this order: the type's own template, then each parent type, then the `*` template, then a built-in default. Parents come from a `TypeParentLookup` function that defaults to "no parent". The editable taxonomy tables plug in by supplying that function, and this module does not depend on them.

Two templates are seeded: `default` (`*`) and `issuer` (financial facts in their own section). `PUT /api/v1/wiki/templates/{id}` files a template as a change set:

- A steward's edit merges straight away (`201`).
- Anyone else's edit waits for review (`202`), under the existing rules.
- A template that fails to parse is reported by `GET /api/v1/wiki/templates`, and pages fall back to the next template in the order above.

`page_template` is added to the Context Hub knowledge folders, so templates export and import with the rest.

## Limitations

- **Templates are read at their current version.** Reading a page at a channel or release pins the claims but not the template.
- **No live Mermaid rendering in the UI.** The panel shows the Mermaid source; the Markdown output renders in any Mermaid-aware viewer.
- **Ask finds pages by lexical search.** It uses entity hits and the subjects of matching claims, so it reads at most three pages per question.
- **The narration cache is in-process.** With several app servers, each keeps its own cache. That is correct, because cache keys are fingerprints, but it repeats model calls.
