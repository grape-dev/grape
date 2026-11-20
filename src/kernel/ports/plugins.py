"""Plugin capability surface (market/negotiation state).

collect_plugin_capabilities and friends live in the runtime domain;
kernel reaches them only through this port (bound by src/api/contracts_wiring.py).
"""

from __future__ import annotations

from typing import Any

from src.kernel.ports._base import PortNotBound, _bind_registry

_NAME = "plugins"


def bind(impl: Any, *, replace: bool = False) -> None:
    """Bind the domain implementation. Called only by the assembly layer."""
    _bind_registry().bind(_NAME, impl, replace=replace)


def is_bound() -> bool:
    try:
        _bind_registry().get(_NAME)
        return True
    except PortNotBound:
        return False


def __getattr__(name: str) -> Any:
    """Forward `kernel.ports.plugins.<symbol>` to the bound implementation."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_bind_registry().get(_NAME), name)
