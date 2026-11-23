"""Non-sensitive SSE event bus for live UI epoch/conflict/snapshot notifications (ADR-005)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, Literal

from context_core.compression import format_utc, utc_now

AllowedEventType = Literal[
    "epoch_updated",
    "claim_written",
    "conflict_resolved",
    "snapshot_published",
]


class EventBus:
    """In-process pub/sub emitting ONLY non-sensitive structural notifications over SSE."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, event_type: AllowedEventType, payload: dict[str, Any]) -> None:
        """Broadcast a sanitized non-sensitive event to all active SSE listeners."""
        event = {
            "event": event_type,
            "data": {
                **payload,
                "timestamp": format_utc(utc_now()),
            },
        }
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncGenerator[dict[str, str], None]:
        """Yield SSE-formatted events cleanly until client disconnects or server shuts down."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        try:
            yield {
                "event": "epoch_updated",
                "data": json.dumps({"status": "connected", "timestamp": format_utc(utc_now())}),
            }
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {
                        "event": item["event"],
                        "data": json.dumps(item["data"]),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": format_utc(utc_now())})}
        finally:
            self._subscribers.discard(queue)

    def clear(self) -> None:
        self._subscribers.clear()
