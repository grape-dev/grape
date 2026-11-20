"""Kernel-side ports: the capabilities kernel needs from business domains.

Why this exists
---------------
R1 says `src/kernel` must not import `src.domains.*`. Historically that rule only
covered module-level imports, so 132 deferred (in-function) imports across 17 kernel
files stayed invisible to it. This package inverts the dependency instead of just
hiding it: kernel declares what it needs as a *port* (a Protocol), and the assembly
layer (`src/api/contracts_wiring.py`, which is allowed to depend on domains) binds
the domain implementation to that port at startup.

    kernel code:   from src.kernel.ports import tokens
                   tokens.task_token(task)          # no domain import anywhere
    assembly:      from src.kernel.ports import tokens
                   tokens.bind(TokenPort(impl=mgmt_impl))   # domains -> kernel only

Direction of dependency after the inversion:

    kernel  <-  api (assembly)  ->  domains
       ^                                  |
       |_____________ ports ______________|

Kernel depends on nothing but its own ports. Domains never import kernel ports to
discover each other; only the assembly layer binds implementations.

Unbound-port behaviour
----------------------
A port that was never bound raises `PortNotBound` with the port name. This is
deliberate fail-fast: silently returning None made the old deferred imports fail
late and far from the cause (the classic "returns empty list, no error" symptom we
chased for days on the migration work).
"""

from __future__ import annotations

from src.kernel.ports._base import Port, PortNotBound, clear_bindings, get_binding

__all__ = ["Port", "PortNotBound", "clear_bindings", "get_binding"]
# ── 能力端口 ────────────────────────────────────────────────────────────────
# 每个端口一个模块；kernel 代码只 import 这些模块，从不 import src.domains。
# 绑定在 src/api/contracts_wiring.py 完成（组装层，允许依赖域）。
# 用相对导入：写成 `from src.kernel.ports import x` 会形成 ports <-> ports.x 的
# 自引用环（check-arch R5 会红）。
from . import (  # noqa: E402
    accounts,
    engines,
    im,
    oauth,
    pipeline,
    repos,
    tokens,
    vcs_cards,
)

__all__ += [
    "accounts", "engines", "im", "oauth", "pipeline", "repos", "tokens", "vcs_cards",
]
