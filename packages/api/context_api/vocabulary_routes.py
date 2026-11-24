"""Editable taxonomy endpoints (ADR-008).

Reading is open to any signed-in caller: vocabulary terms carry no wall side or read groups.
Every edit, including approving a term that was registered as proposed, goes through the change-set
flow of ADR-007, so the approval rules decide whether it merges straight away or waits for a steward.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from context_core.exceptions import NotFoundError
from context_core.models import Principal
from context_core.versioning import ChangeItemInput
from context_core.vocabulary import VOCABULARIES, VocabStatus, normalize_term_key, term_id_for

from context_api.versioning_routes import _after_write, _principal, _svc

router = APIRouter()


def _store(request: Request) -> Any:
    return request.app.state.runtime.registry.store


def _tree(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {t["key"]: {**t, "children": []} for t in terms}
    roots: list[dict[str, Any]] = []
    for node in by_key.values():
        parent = node.get("parent_key")
        if parent and parent in by_key:
            by_key[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/api/v1/vocabularies")
async def list_vocabularies(request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    terms = await _store(request).list_vocab_terms()
    counts: dict[str, dict[str, int]] = {}
    for t in terms:
        c = counts.setdefault(t.vocab, {"active": 0, "proposed": 0, "deprecated": 0})
        c[t.status] = c.get(t.status, 0) + 1
    names = sorted(set(VOCABULARIES) | set(counts))
    return {
        "vocabularies": [
            {"vocab": n, "description": VOCABULARIES.get(n, ""), "counts": counts.get(n, {"active": 0, "proposed": 0, "deprecated": 0})}
            for n in names
        ]
    }


@router.get("/api/v1/vocabularies/proposed")
async def list_proposed_terms(request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    """Terms first seen in data and waiting for a steward, across every vocabulary."""
    terms = await _store(request).list_vocab_terms(statuses=["proposed"])
    return {"terms": [t.model_dump(mode="json") for t in terms]}


@router.get("/api/v1/vocabularies/{vocab}/terms")
async def list_terms(
    vocab: str,
    request: Request,
    status: list[VocabStatus] | None = Query(default=None),
    tree: bool = False,
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    terms = [t.model_dump(mode="json") for t in await _store(request).list_vocab_terms(vocab=normalize_term_key(vocab), statuses=status)]
    return {"vocab": normalize_term_key(vocab), "terms": _tree(terms) if tree else terms}


@router.get("/api/v1/vocabularies/{vocab}/terms/{key}")
async def get_term(vocab: str, key: str, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    store = _store(request)
    v, k = normalize_term_key(vocab), normalize_term_key(key)
    term = await store.get_vocab_term(term_id_for(v, k))
    if term is None:
        raise NotFoundError(f"Unknown term {v}:{k}")
    idx = await store.vocabulary_index()
    return {
        "term": term.model_dump(mode="json"),
        "ancestors": idx.ancestors(v, k),
        "narrower": sorted(idx.descendants(v, k) - {k}),
    }


class TermEditBody(BaseModel):
    """Fields to set on the term. Omitted fields keep their current value."""

    label: str | None = None
    parent_key: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    status: VocabStatus | None = None
    merged_into_key: str | None = None
    attributes: dict[str, Any] | None = None
    owner_group: str | None = None
    reason: str = ""
    title: str | None = Field(default=None, description="Change set title; defaults to a description of the edit")


@router.put("/api/v1/vocabularies/{vocab}/terms/{key}")
async def propose_term_edit(
    vocab: str, key: str, body: TermEditBody, request: Request, principal: Principal = Depends(_principal)
) -> dict[str, Any]:
    """Add or edit a term through a change set. Returns the change set (merged or waiting for approval)."""
    svc = _svc(request)
    v, k = normalize_term_key(vocab), normalize_term_key(key)
    fields = body.model_dump(exclude_none=True, exclude={"reason", "title"})
    if body.parent_key == "":
        fields["parent_key"] = None
    action = {"active": "Approve", "deprecated": "Deprecate"}.get(body.status or "", "Edit")
    cs = await svc.propose(
        title=body.title or f"{action} term {v}:{k}",
        reason=body.reason,
        items=[ChangeItemInput(record_type="vocab_term", op="upsert", record_id=term_id_for(v, k), payload=fields)],
        principal=principal,
    )
    if cs.status == "merged":
        await _after_write(request, "vocab_term_changed")
    return {"change_set": svc.view(cs, principal)}


__all__ = ["router"]
