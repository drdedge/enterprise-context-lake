"""Vocabulary behaviour shared by every store adapter (ADR-008).

A store mixes in ``VocabularyStoreMixin`` and implements three storage primitives:

- ``_vocab_load()``: every stored term
- ``_vocab_save(terms)``: insert or replace these terms
- ``_vocab_count()``: how many terms are stored

The mixin supplies the StorePort vocabulary methods, seeds the default terms into an empty store,
registers unknown terms as proposed, and expands entity-type filters down the tree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from context_core.compression import utc_now
from context_core.exceptions import NotFoundError
from context_core.vocabulary import (
    VocabTerm,
    VocabularyIndex,
    default_terms,
    proposed_term,
    term_id_for,
    terms_used,
    terms_used_in_state,
    validate_term_change,
)


class VocabularyStoreMixin:
    _vocab_index_cache: VocabularyIndex | None = None
    _new_vocab_terms: list[VocabTerm]

    # -- storage primitives (implemented by each store) -------------------------------------------

    async def _vocab_load(self) -> list[VocabTerm]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _vocab_save(self, terms: Sequence[VocabTerm]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _vocab_count(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------------------------------

    def _vocab_pending(self) -> list[VocabTerm]:
        pending = getattr(self, "_new_vocab_terms", None)
        if pending is None:
            pending = []
            self._new_vocab_terms = pending
        return pending

    def _vocab_invalidate(self) -> None:
        self._vocab_index_cache = None

    async def _seed_default_vocab(self) -> None:
        if await self._vocab_count() == 0:
            await self._vocab_save(default_terms())
            self._vocab_invalidate()

    async def vocabulary_index(self) -> VocabularyIndex:
        idx = self._vocab_index_cache
        if idx is None:
            idx = VocabularyIndex(await self._vocab_load())
            self._vocab_index_cache = idx
        return idx

    async def _register_terms(self, pairs: Iterable[tuple[str, str]], *, created_by: str = "system") -> list[VocabTerm]:
        """Store any (vocab, key) not yet known as a proposed term. Returns the new terms."""
        idx = await self.vocabulary_index()
        new: dict[tuple[str, str], VocabTerm] = {}
        for vocab, key in pairs:
            if not key or idx.is_known(vocab, key) or (vocab, key) in new:
                continue
            new[(vocab, key)] = proposed_term(vocab, key, created_by=created_by)
        if not new:
            return []
        terms = list(new.values())
        await self._vocab_save(terms)
        self._vocab_invalidate()
        self._vocab_pending().extend(terms)
        return terms

    async def _register_record_terms(self, record_kind: str, record: Any, *, created_by: str = "system") -> None:
        await self._register_terms(terms_used(record_kind, record), created_by=created_by)

    async def _register_state_terms(self, state: dict[str, Any]) -> None:
        await self._register_terms(terms_used_in_state(state), created_by="system:import")

    async def _import_vocab_state(self, state: dict[str, Any]) -> None:
        """Bulk import: replace terms if the state carries them, then register any still unknown."""
        terms = state.get("vocab_terms")
        if terms:
            await self._vocab_replace_all([t if isinstance(t, VocabTerm) else VocabTerm.model_validate(t) for t in terms])
            self._vocab_invalidate()
        else:
            await self._seed_default_vocab()
        await self._register_state_terms(state)

    async def _vocab_replace_all(self, terms: Sequence[VocabTerm]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def drain_new_vocab_terms(self) -> list[VocabTerm]:
        """Terms registered since the last call, so the version ledger can record them."""
        pending = self._vocab_pending()
        out = list(pending)
        pending.clear()
        return out

    async def expand_entity_types(self, entity_types: Sequence[str] | None) -> set[str] | None:
        if not entity_types:
            return None
        return (await self.vocabulary_index()).expand("entity_type", list(entity_types))

    # -- StorePort methods --------------------------------------------------------------------------

    async def list_vocab_terms(
        self, *, vocab: str | None = None, statuses: Sequence[str] | None = None
    ) -> list[VocabTerm]:
        await self._ensure_init()  # type: ignore[attr-defined]
        idx = await self.vocabulary_index()
        wanted = set(statuses) if statuses else None
        return [t.model_copy(deep=True) for t in idx.terms(vocab) if wanted is None or t.status in wanted]

    async def get_vocab_term(self, term_id: str) -> VocabTerm | None:
        await self._ensure_init()  # type: ignore[attr-defined]
        vocab, _, key = term_id.partition(":")
        t = (await self.vocabulary_index()).get(vocab, key)
        return t.model_copy(deep=True) if t else None

    async def upsert_vocab_term(self, term: VocabTerm) -> VocabTerm:
        """Create or edit a term. Callers outside a change set should propose through ChangeSetService."""
        await self._ensure_init()  # type: ignore[attr-defined]
        idx = await self.vocabulary_index()
        validate_term_change(term, idx)
        existing = idx.get(term.vocab, term.key)
        stored = term.model_copy(
            update={
                "created_at": existing.created_at if existing else term.created_at,
                "created_by": existing.created_by if existing else term.created_by,
                "updated_at": utc_now(),
            }
        )
        await self._vocab_save([stored])
        self._vocab_invalidate()
        return stored.model_copy(deep=True)

    async def require_vocab_term(self, vocab: str, key: str) -> VocabTerm:
        t = await self.get_vocab_term(term_id_for(vocab, key))
        if t is None:
            raise NotFoundError(f"Unknown term {vocab}:{key}")
        return t


__all__ = ["VocabularyStoreMixin"]
