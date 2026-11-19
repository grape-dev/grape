"""Assembly layer: bind domain implementations to kernel ports.

This module lives in `src/api` precisely because that layer *is* allowed to depend on
`src.domains`. Kernel code imports only `src.kernel.ports.*`; the dependency arrow
points domains -> api -> kernel-ports, never kernel -> domains.

Called once during process startup, from `src/api/db_setup.py` / process entry, before
any request is served. Binding is idempotent per process (replace=True) so repeated
startup paths (frontend build, worker bootstrap, tests) do not explode.

Why aggregate objects instead of binding each domain module directly
-------------------------------------------------------------------
A port bundles the capabilities kernel needs for one concern, and those capabilities
often live in several domain modules (tokens span `mgmt.user.db`,
`mgmt.auth.credential_crypto` and `runtime.public`). Binding the modules themselves and
letting the port resolve across them keeps the port surface stable: a symbol moving
between domain modules does not change kernel code.

Resolution is attribute-by-attribute and deliberately lenient across the candidate
list — but a symbol missing from *every* candidate raises AttributeError naming all the
places it looked, instead of returning None.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from src.kernel.ports import (
    accounts,
    engines,
    im,
    oauth,
    pipeline,
    repos,
    tokens,
    vcs_cards,
)

log = logging.getLogger(__name__)


class _Aggregate:
    """Attribute proxy across an ordered list of domain modules.

    First candidate that has the attribute wins, in the declared order. Order matters
    when the same name exists in two modules; callers declare the authoritative module
    first. A miss raises AttributeError listing every candidate searched, so a rename
    shows up as a clear startup/call error rather than a silent None.
    """

    __slots__ = ("_loaders", "_names", "_cache")

    def __init__(self, loaders: Sequence[tuple[str, Callable[[], Any]]]) -> None:
        self._loaders = list(loaders)
        self._names = [n for n, _ in loaders]
        self._cache: dict[str, Any] = {}

    def _module(self, label: str) -> Any:
        mod = self._cache.get(label)
        if mod is None:
            loader = dict(self._loaders)[label]
            mod = loader()
            self._cache[label] = mod
        return mod

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        for label in self._names:
            try:
                mod = self._module(label)
            except Exception as exc:                      # import-time failure
                log.warning("port candidate %s failed to load: %s", label, exc)
                continue
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"{name!r} not found in any port candidate: {', '.join(self._names)}"
        )

    def __dir__(self) -> list[str]:
        names: set[str] = set()
        for label in self._names:
            try:
                names.update(dir(self._module(label)))
            except Exception:
                continue
        return sorted(names)


def _lazy(dotted: str) -> Callable[[], Any]:
    def _load() -> Any:
        import importlib

        return importlib.import_module(dotted)

    _load.__name__ = f"load_{dotted.replace('.', '_')}"
    return _load


# ── 端口 → 域模块候选（顺序即优先级）────────────────────────────────────────
# 每次 bind 只传模块路径，不在 import 期真的导入域：装配完成前任何 kernel 调用
# 都会撞 PortNotBound，而不是撞半初始化的模块。
PORT_TARGETS: dict[str, tuple[str, ...]] = {
    "tokens": (
        "src.domains.mgmt.user.db",
        "src.domains.mgmt.auth.credential_crypto",
        "src.domains.mgmt.auth.grape_account_db",
        "src.domains.runtime.public",
        # 公开包装（bot cache invalidation / repo secret decrypt）只在此模块；
        # 排在最后，先命中的候选优先，不改变既有符号解析。
        "src.domains.mgmt.public",
    ),
    "repos": (
        "src.domains.mgmt.user.repo_manager",
        "src.domains.mgmt.public",
        # 仓库级联清理要读技能根目录常量 SKILL_DIR（skills/repo/{repo_key} 的父目录）；
        # 该常量只在 mgmt.user.db 定义，排最后不影响上面两个候选的既有符号解析。
        "src.domains.mgmt.user.db",
    ),
    "accounts": (
        "src.domains.mgmt.auth.grape_account_db",
        "src.domains.mgmt.auth.security_config",
        "src.domains.mgmt.auth",
        "src.domains.mgmt.public",
    ),
    "oauth": (
        "src.domains.mgmt.auth.providers",
        "src.domains.mgmt.public",
    ),
    "vcs_cards": (
        "src.domains.runtime.vcs.platform_card",
        "src.domains.runtime.vcs.providers",
        "src.domains.runtime.vcs.token_error",
        "src.domains.runtime.public",
    ),
    "engines": (
        "src.domains.runtime.engine.engine_select",
        "src.domains.runtime.engine.instances",
        "src.domains.runtime.engine.service",
        "src.domains.runtime.public",
        # 引擎解析的 id/名称映射原语（is_known_engine_name / get_runtime_id_by_name /
        # ensure_default_backend / get_local_direct_runtime_id）只在该模块；排最后，
        # 前面候选先命中，既有符号解析不变。
        "src.domains.mgmt.user.db",
    ),
    "im": (
        "src.domains.runtime.im.feishu_async",
        "src.domains.runtime.public",
    ),
    "pipeline": (
        "src.domains.runtime.pipeline.event_pipeline",
        "src.domains.runtime.engine.repogit",
        "src.domains.runtime.public",
    ),
}

_PORTS = {
    "tokens": tokens,
    "repos": repos,
    "accounts": accounts,
    "oauth": oauth,
    "vcs_cards": vcs_cards,
    "engines": engines,
    "im": im,
    "pipeline": pipeline,
}


def bind_all(*, replace: bool = True) -> list[str]:
    """Bind every port. Returns the names actually bound."""
    bound: list[str] = []
    for name, port in _PORTS.items():
        targets = PORT_TARGETS[name]
        agg = _Aggregate([(t, _lazy(t)) for t in targets])
        port.bind(agg, replace=replace)
        bound.append(name)
    log.info("kernel ports bound: %s", ", ".join(bound))
    return bound


def ensure_bound() -> None:
    """Bind once, tolerating concurrent/duplicate startup paths."""
    for name, port in _PORTS.items():
        if not port.is_bound():
            bind_all(replace=True)
            return