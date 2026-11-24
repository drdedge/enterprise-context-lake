"""Model-backed narrators for entity pages (ADR-009).

Two kinds of model can write a page overview or an answer:

- ``GeminiNarrator`` calls the Gemini API. It is **external**, so the wiki service only ever gives
  it public-side pages, and it can never be configured for the private side.
- ``InternalChatNarrator`` calls an OpenAI-compatible chat endpoint that the firm runs inside its own
  boundary (for example a model served on the firm's AWS account). It is **internal**, so it may
  write private-side and above-the-wall pages. Marking an endpoint internal is an operator decision:
  only point ``CONTEXT_WIKI_INTERNAL_MODEL_URL`` at infrastructure that MNPI may reach.

Configuration (all optional; without it everything is written by the deterministic narrator):

  CONTEXT_WIKI_NARRATOR          deterministic | gemini | internal   (public-side pages)
  CONTEXT_WIKI_PRIVATE_NARRATOR  deterministic | internal            (private-side and above the wall)
  CONTEXT_WIKI_INTERNAL_MODEL_URL, CONTEXT_WIKI_INTERNAL_MODEL, CONTEXT_WIKI_INTERNAL_MODEL_KEY

``LLM_MODE=deterministic`` (or the runtime's deterministic mode) turns every model off.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from context_core.model_boundary import ModelBoundary
from context_core.models import Entity
from context_core.wiki import NarratorConfig, NumberedStatement, PageNarrator

SYSTEM_PROMPT = (
    "You write short, factual wiki text for an institutional knowledge base. Use ONLY the numbered "
    "statements provided. Every sentence or bullet must end with the number of each statement it "
    "relies on, in square brackets, like [3] or [2, 5]. Never cite a number that was not provided. "
    "Do not speculate, add outside knowledge, or mention these instructions."
)


def _statement_block(statements: list[NumberedStatement]) -> str:
    return "\n".join(f"[{s.n}] {s.text}" for s in statements)


def overview_prompt(entity: Entity, statements: list[NumberedStatement]) -> str:
    return (
        f"Entity: {entity.display_name} ({entity.entity_type.value})\n"
        f"Description: {entity.description or '(none)'}\n\nStatements:\n{_statement_block(statements[:60])}\n\n"
        "Write a 2 to 4 sentence overview of this entity."
    )


def answer_prompt(question: str, statements: list[NumberedStatement]) -> str:
    return (
        f"Statements:\n{_statement_block(statements[:120])}\n\nQuestion: {question}\n\n"
        "Answer in at most 5 sentences or bullets. If the statements do not answer the question, "
        "say so in one sentence that cites the closest statement."
    )


class GeminiNarrator:
    """External model. Public-side pages only (enforced by WikiService and NarratorConfig)."""

    name = "gemini"
    boundary: ModelBoundary = "external"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def _complete(self, prompt: str) -> str | None:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(model=self._model, google_api_key=self._api_key, temperature=0.0)
        resp = await llm.ainvoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
        content = resp.content
        return content.strip() if isinstance(content, str) and content.strip() else None

    async def overview(self, *, entity: Entity, statements: list[NumberedStatement]) -> str | None:
        return await self._complete(overview_prompt(entity, statements))

    async def answer(self, *, question: str, statements: list[NumberedStatement]) -> str | None:
        return await self._complete(answer_prompt(question, statements))


class InternalChatNarrator:
    """Internal model behind an OpenAI-compatible ``/chat/completions`` endpoint run by the firm."""

    name = "internal"
    boundary: ModelBoundary = "internal"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def _complete(self, prompt: str) -> str | None:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(self._url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        return content.strip() if isinstance(content, str) and content.strip() else None

    async def overview(self, *, entity: Entity, statements: list[NumberedStatement]) -> str | None:
        return await self._complete(overview_prompt(entity, statements))

    async def answer(self, *, question: str, statements: list[NumberedStatement]) -> str | None:
        return await self._complete(answer_prompt(question, statements))


def _internal_from_env() -> InternalChatNarrator | None:
    url = os.environ.get("CONTEXT_WIKI_INTERNAL_MODEL_URL", "").strip()
    model = os.environ.get("CONTEXT_WIKI_INTERNAL_MODEL", "").strip()
    if not url or not model:
        return None
    return InternalChatNarrator(url, model, api_key=os.environ.get("CONTEXT_WIKI_INTERNAL_MODEL_KEY") or None)


def narrators_from_env(llm_mode: str = "auto") -> NarratorConfig:
    """Build the narrator configuration. Anything unset or unavailable falls back to deterministic."""
    if (llm_mode or "").lower() == "deterministic" or os.environ.get("LLM_MODE", "").lower() == "deterministic":
        return NarratorConfig()
    public_choice = os.environ.get("CONTEXT_WIKI_NARRATOR", "deterministic").strip().lower()
    private_choice = os.environ.get("CONTEXT_WIKI_PRIVATE_NARRATOR", "deterministic").strip().lower()

    public: PageNarrator | None = None
    if public_choice == "gemini":
        key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        public = GeminiNarrator(key) if key else None
    elif public_choice == "internal":
        public = _internal_from_env()

    private: PageNarrator | None = None
    if private_choice == "internal":
        private = _internal_from_env()
    elif private_choice not in ("", "deterministic"):
        raise ValueError(
            f"CONTEXT_WIKI_PRIVATE_NARRATOR={private_choice!r} is not allowed: private-side pages use "
            "'deterministic' or 'internal' only"
        )
    return NarratorConfig(public=public, private=private)
