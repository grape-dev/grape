"""Port base: a named capability slot that the assembly layer binds at startup."""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable


class PortNotBound(RuntimeError):
    """Raised when kernel code uses a port the assembly layer never bound.

    Fail fast and loud: the previous pattern (deferred import inside a try/except,
    or returning None on failure) turned a missing dependency into a silent empty
    result far from the real cause.
    """

    def __init__(self, name: str, hint: str = "") -> None:
        msg = (
            f"kernel port {name!r} is not bound. The assembly layer "
            f"(src/api/contracts_wiring.py) must bind it during startup."
        )
        if hint:
            msg = f"{msg} {hint}"
        super().__init__(msg)
        self.name = name


@runtime_checkable
class Port(Protocol):
    """Structural marker for port implementations.

    Ports are plain objects; this protocol exists so `isinstance(x, Port)` in
    tests can assert 'this looks like a port binding' without importing each
    concrete class.
    """

    def __port_name__(self) -> str:  # pragma: no cover - marker only
        ...


_POST_BIND_HOOKS: list[Any] = []


def on_bind(hook: Any) -> None:
    """Register a kernel-side initialiser that must run once implementations exist.

    Kernel modules used to do work at import time by calling a domain function
    directly (schema bootstrap on the caller's connection). After the inversion the
    same work has to go through a port, which is unbound at import time — resolving
    it then would raise PortNotBound and break every early importer. Such a module
    registers its deferred initialiser here instead; the assembly layer's `bind()`
    calls fire it. Ports themselves stay fail-fast: a *use* while unbound still raises.
    """
    _POST_BIND_HOOKS.append(hook)


class _Registry:
    """Process-wide port registry.

    Bindings are process-scoped, not thread-scoped: every process (mgmt / event /
    worker / insight) binds the same set during startup, and the binding never
    changes afterwards. Reads therefore need no lock; only bind/unbind do.
    """

    __slots__ = ("_bindings", "_lock")

    def __init__(self) -> None:
        self._bindings: dict[str, Any] = {}
        self._lock = threading.Lock()

    def bind(self, name: str, impl: Any, *, replace: bool = False) -> None:
        with self._lock:
            if name in self._bindings and not replace:
                raise RuntimeError(
                    f"kernel port {name!r} bound twice; pass replace=True if a test "
                    f"is rebinding it deliberately"
                )
            self._bindings[name] = impl
        # 在绑钩子在锁外执行：钩子是 kernel 侧的延后初始化（例如 import 期不能碰端口的
        # 建表引导），它们会再次读取端口，持锁调用会给自己上锁。
        for hook in tuple(_POST_BIND_HOOKS):
            hook(name)

    def get(self, name: str) -> Any:
        impl = self._bindings.get(name)
        if impl is None:
            raise PortNotBound(name)
        return impl

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def bound_names(self) -> list[str]:
        return sorted(self._bindings)


_REGISTRY = _Registry()


def get_binding(name: str) -> Any:
    """Return the bound implementation for `name`, or raise PortNotBound."""
    return _REGISTRY.get(name)


def clear_bindings() -> None:
    """Test helper: forget every binding (tests must rebind their fakes)."""
    _REGISTRY.clear()


def bound_names() -> list[str]:
    return _REGISTRY.bound_names()


def _bind_registry() -> _Registry:
    """Internal accessor used by port modules and the assembly wiring."""
    return _REGISTRY