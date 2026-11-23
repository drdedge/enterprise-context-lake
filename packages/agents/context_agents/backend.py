"""Re-export alias for ContextLakeBackend (`context_agents.backend` -> `context_agents.backends`)."""

from context_agents.backends import (
    ContextLakeBackend,
    create_context_composite_backend,
    load_skill_include_tools,
)

__all__ = [
    "ContextLakeBackend",
    "create_context_composite_backend",
    "load_skill_include_tools",
]
