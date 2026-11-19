"""Account identity, expiry and security config.

Grape account lookups, identity expiry, JWT key material, production flag.
"""

from __future__ import annotations

from typing import Any

from src.kernel.ports._base import PortNotBound, _bind_registry

_NAME = "accounts"


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
    """Forward `kernel.ports.accounts.<symbol>` to the bound implementation.

    Module-level forwarding (PEP 562) keeps the port surface open without
    enumerating every capability: adding a symbol to the domain implementation
    does not require touching this file. Unbound access still raises PortNotBound
    from the registry, so a missing bind fails at the call site, not later.
    """
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_bind_registry().get(_NAME), name)
