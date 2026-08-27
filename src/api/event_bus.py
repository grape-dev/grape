"""Event Bus (event-dispatch-redesign-v2.md Phase 4).

Implements the 4 DSH event dispatch modes adapted for Grape's Python runtime:
- emit: fire-and-forget notification, all listeners invoked, no veto
- waterfall: middleware chain, each listener calls next(), can intercept/modify
- serial: sequential execution, first bail value wins
- parallel: all listeners run concurrently, wait for all to complete

Three event domains:
- PIPELINE: pipeline/node/* (pipeline lifecycle)
- AGENT: agent/handoff, agent/start, agent/complete (agent lifecycle)
- CAPABILITY: skill/deliver, engine/select (resource lifecycle)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

log = logging.getLogger("devops-bot")


class EventDomain(str, Enum):
    PIPELINE = "pipeline"
    AGENT = "agent"
    CAPABILITY = "capability"


@dataclass
class GrapeEvent:
    """Structured event carrying domain, type, payload, and routing context."""
    domain: EventDomain
    type: str                    # e.g. "agent/handoff", "pipeline/node/queued"
    repo_key: str = ""
    issue_id: str = ""
    agent_type: str = ""
    seq: int = 0
    payload: dict = field(default_factory=dict)


# Listener type: async function (event, next_fn?) -> Any
Listener = Callable[..., Coroutine]


class EventBus:
    """In-process async event bus with 4 dispatch modes.

    Not a distributed message queue — all listeners are in-process Python coroutines.
    Thread-safe for listener registration; dispatch runs on the caller's event loop.
    """

    def __init__(self):
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = asyncio.Lock()
        self._history: list[dict] = []  # event dispatch history (last 100)

    async def on(self, event_type: str, listener: Listener):
        """Register a listener for an event type."""
        async with self._lock:
            if event_type not in self._listeners:
                self._listeners[event_type] = []
            self._listeners[event_type].append(listener)

    def on_sync(self, event_type: str, listener: Listener):
        """Register listener synchronously (for startup init)."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(listener)

    def _get_listeners(self, event_type: str) -> list[Listener]:
        return self._listeners.get(event_type, [])

    async def emit(self, event: GrapeEvent):
        """Fire-and-forget: invoke all listeners, contain errors per-listener.

        No listener can veto lifecycle progress. Returned promises are awaited
        but rejections are logged and contained.
        """
        self._record(event, "emit")
        listeners = self._get_listeners(event.type)
        for fn in listeners:
            try:
                result = fn(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.warning(f"event_bus emit '{event.type}' listener failed: {e}")

    async def waterfall(self, event: GrapeEvent, initial_value: Any = None) -> Any:
        """Middleware chain: each listener receives (event, next_fn) and must call next().

        A listener can intercept by NOT calling next(), returning its own value.
        Returns the final composed value.
        """
        listeners = self._get_listeners(event.type)
        if not listeners:
            return initial_value

        # Build the chain from inside out
        async def _terminal(_event, _value):
            return _value

        chain = _terminal
        for fn in reversed(listeners):
            def _wrap(next_fn, listener):
                async def _wrapped(ev, val):
                    return await listener(ev, val, next_fn)
                return _wrapped
            chain = _wrap(chain, fn)

        return await chain(event, initial_value)

    async def serial(self, event: GrapeEvent, initial_value: Any = None) -> Any:
        """Sequential dispatch: listeners called in order, first bail wins.

        If a listener returns a non-None value, dispatch stops and that value is returned.
        """
        listeners = self._get_listeners(event.type)
        value = initial_value
        for fn in listeners:
            try:
                result = fn(event, value)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    return result
            except Exception as e:
                log.warning(f"event_bus serial '{event.type}' listener failed: {e}")
        return value

    async def parallel(self, event: GrapeEvent) -> list[Any]:
        """Concurrent dispatch: all listeners start simultaneously, wait for all.

        Exceptions are contained per-listener (resolved to None).
        """
        listeners = self._get_listeners(event.type)
        if not listeners:
            return []

        async def _safe(fn):
            try:
                result = fn(event)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            except Exception as e:
                log.warning(f"event_bus parallel '{event.type}' listener failed: {e}")
                return None

        results = await asyncio.gather(*[_safe(fn) for fn in listeners])
        return list(results)

    def has_listeners(self, event_type: str) -> bool:
        return len(self._get_listeners(event_type)) > 0

    def _record(self, event: GrapeEvent, mode: str):
        """Record event dispatch to history (capped at 100)."""
        import time as _t
        self._history.append({
            "type": event.type,
            "domain": event.domain.value if hasattr(event.domain, "value") else str(event.domain),
            "repo_key": event.repo_key,
            "issue_id": event.issue_id,
            "agent_type": event.agent_type,
            "seq": event.seq,
            "mode": mode,
            "listeners": len(self._get_listeners(event.type)),
            "ts": _t.time(),
        })
        if len(self._history) > 100:
            self._history = self._history[-100:]

    def get_history(self) -> list[dict]:
        """Return event dispatch history."""
        return self._history


# Singleton
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global EventBus singleton."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
