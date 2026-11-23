"""Default ASGI entrypoint for Uvicorn (`uvicorn context_api.main:app --host 127.0.0.1 --port 8000`)."""

from __future__ import annotations

import os

from context_api.app import create_app

app = create_app(
    db_path=os.environ.get("CONTEXT_LAKE_DB") or ":memory:",
    llm_mode=os.environ.get("CONTEXT_LAKE_LLM_MODE", "auto"),  # type: ignore[arg-type]
)
