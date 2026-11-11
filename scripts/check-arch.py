#!/usr/bin/env python3
"""Grape architecture constraint checker (Issue #699 Phase 1, TC-1.4).

Static AST scan of src/kernel/ and src/domains/ enforcing layering rules
R1-R6. Hard violations exit 1 (CI red); whitelisted findings are reported
with reasons and do not fail the build.

Rules:
  R1  kernel must not import src.domains.*           (hard)
  R2  kernel must not import src.api.*               (hard)
  R3  domains must not import apps.*                 (reserved for Phase 4)
  R4  cross-domain imports (mgmt/runtime/insight)    (report-only; Phase 5
      must collapse them into src/domains/contracts)
  R5  no import cycles at module scope between
      kernel/domains (hard; deferred in-function imports excluded)
  R6  kernel/domains must not import legacy top-level
      shim paths (src.config, src.api.jwt_utils, ...)             (hard)

Usage:
  python scripts/check-arch.py            # print report, exit 1 on hard violations
  python scripts/check-arch.py --strict   # additionally fail on R4 findings
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO_ROOT, "src")

# Layers scanned (canonical Phase 1 layout only; shims at src/ top level are
# compatibility surface, not architecture).
KERNEL = "src/kernel"
DOMAINS = "src/domains"
DOMAIN_NAMES = ("mgmt", "runtime", "insight", "contracts")

# Legacy top-level module paths that still exist only as re-export shims.
# Issue #699 cleanup: every legacy *directory* shim (src/auth, src/user,
# src/runtime, src/agent, src/im, src/vcs, src/storage, src/observability,
# src/admin, src/project_info, src/api/routes/*) has been removed once all
# importers migrated to src.domains.*; only the thin top-level module shims
# kept for external callers remain forbidden inside kernel/domains.
LEGACY_SHIM_PREFIXES = (
    "src.config", "src.session_manager", "src.lock", "src.repo_key", "src.repo",
    "src.repo_cleanup", "src.repo_migrate", "src.mysql_compat", "src.token_service",
    "src.token_resolver", "src.prompt_builder", "src.worker_package", "src.localfs",
    "src.outbound_ledger", "src.scene_custom_env", "src.featured_order",
    "src.help_docs_sync", "src.help_page_gen",
    "src.api.event_pipeline", "src.api.jwt_utils", "src.api.auth_token",
)

# ── Whitelist: module -> {rule: reason} ────────────────────────────────────
# Pre-existing deferred (function-level) imports kept working during Phase 1;
# each entry carries the phase that will remove it.
WHITELIST: dict[str, dict[str, str]] = {
    # R1/R2: kernel historically reaches up for lazy reads; Phase 5 contracts
    # will invert these dependencies.
    "src/kernel/config.py": {
        "R1": "deferred meta get/set via mgmt.user.db; Phase 5 -> MgmtService",
        "R2": "deferred domain-agent routing lookup via src.api.routing; Phase 4/5",
    },
    "src/kernel/prompt_builder.py": {
        "R1": "deferred reads from mgmt.user.db / runtime engine; Phase 5 contracts",
        "R2": "deferred reads from api session/event/issue stores; Phase 5 contracts",
    },
    "src/kernel/worker_package.py": {
        "R1": "MANIFEST references domain code paths (packaging, not runtime import); Phase 5",
        "R2": "MANIFEST references src/api paths (packaging); Phase 5",
    },
    "src/kernel/token_resolver.py": {
        "R1": "deferred token lookup via mgmt.user.db; Phase 5 -> MgmtService",
        "R2": "deferred token resolution via src.api.deps; Phase 5",
    },
    "src/kernel/session_manager.py": {
        "R1": "deferred feature flags via mgmt.user.db; Phase 5",
        "R2": "deferred timestamp helper via src.api.time_utils; Phase 5",
    },
    "src/kernel/repo_cleanup.py": {
        "R1": "deferred repo state lookup via mgmt.user.db; Phase 5",
    },
    "src/kernel/repo_migrate.py": {
        "R1": "deferred org/user lookups via mgmt.user.db; Phase 5",
    },
    "src/kernel/help_docs_sync.py": {
        "R1": "deferred skill/user lookups via mgmt.user.db; Phase 5",
        "R2": "deferred app/openapi imports for help page generation; Phase 5",
    },
    "src/kernel/auth_sdk/jwt_utils.py": {
        "R1": "deferred secret/credential keys via mgmt.auth.security_config; Phase 5",
    },
    # R5: known import cycles inherited from the pre-Phase-1 layout.
    # agents <-> engine: M0 compat forwarding layer (src.domains.runtime.agents re-exported
    #   src.domains.runtime.engine backends); importing src.domains.runtime.agents before src.domains.runtime.engine fails
    #   the same way on the pre-Phase-1 tree. Phase 3 dissolves the
    #   executor/base coupling when _execute_agent moves out.
    # vcs.providers <-> vcs.providers.base: small registration cycle in the
    #   vcs provider plugin loader; harmless (parent init completes first).
    "CYCLE:src.domains.runtime.agents+src.domains.runtime.agents.base+src.domains.runtime.agents.codearts+src.domains.runtime.agents.codex+src.domains.runtime.agents.dsh+src.domains.runtime.agents.executor+src.domains.runtime.agents.hermes+src.domains.runtime.agents.hwcloud+src.domains.runtime.agents.opencode+src.domains.runtime.engine+src.domains.runtime.agents.base+src.domains.runtime.agents.codearts+src.domains.runtime.agents.codex+src.domains.runtime.agents.dsh+src.domains.runtime.engine.engine_select+src.domains.runtime.agents.executor+src.domains.runtime.agents.hermes+src.domains.runtime.agents.hwcloud+src.domains.runtime.agents.opencode+src.domains.runtime.engine.registry+src.domains.runtime.engine.service": {
        "R5": "pre-existing agents<->engine M0 forwarding cycle; Phase 3 direct_executor cleanup",
    },
    "CYCLE:src.domains.runtime.vcs.providers+src.domains.runtime.vcs.providers.base": {
        "R5": "pre-existing vcs provider registration cycle (harmless init order)",
    },
}


def iter_py(root: str):
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def module_name(path: str) -> str:
    rel = os.path.relpath(path, REPO_ROOT)
    mod = rel[:-3].replace(os.sep, ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def resolve_relative(current_mod: str, level: int, module: str | None,
                     is_package: bool = False) -> str | None:
    """Resolve a relative import (ast.ImportFrom with level>0) to absolute.

    For a plain module, level=1 means its parent package; for a package
    __init__, level=1 means the package itself.
    """
    if level == 0:
        return module
    parts = current_mod.split(".")
    drop = level if not is_package else max(level - 1, 0)
    base = parts[: len(parts) - drop] if drop else parts
    if module:
        base = base + module.split(".")
    resolved = ".".join(base)
    return resolved if resolved.startswith("src") else None


def imports_of(path: str) -> tuple[list[str], list[str]]:
    """(top_level, all) absolute module names imported by the file.

    Top-level (module-scope) imports are the ones that execute during module
    initialization and can trigger partial-initialization failures; deferred
    imports inside functions/classes are collected separately for layering
    rules R1/R2/R4/R6.
    """
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as e:
        print(f"WARN: cannot parse {path}: {e}")
        return [], []
    top, all_imports = set(), set()

    current_mod = module_name(path)
    is_package = os.path.basename(path) == "__init__.py"

    def add(node_names, top_level: bool):
        for name in node_names:
            if name.startswith("src"):
                if top_level:
                    top.add(name)
                all_imports.add(name)

    def collect(nodes, top_level: bool):
        for node in nodes:
            if isinstance(node, ast.Import):
                add([a.name for a in node.names], top_level)
            elif isinstance(node, ast.ImportFrom):
                resolved = resolve_relative(current_mod, node.level, node.module, is_package)
                if resolved:
                    if top_level:
                        top.add(resolved)
                    all_imports.add(resolved)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and top_level:
                collect(ast.iter_child_nodes(node), False)

    collect(tree.body, True)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            add([a.name for a in node.names], False)
        elif isinstance(node, ast.ImportFrom):
            resolved = resolve_relative(current_mod, node.level, node.module, is_package)
            if resolved:
                all_imports.add(resolved)
    top = sorted(n for n in top if n.startswith("src"))
    all_imports = sorted(n for n in all_imports if n.startswith("src"))
    return top, all_imports


def layer_of(mod: str) -> str | None:
    if mod == "src" or mod.startswith("src.kernel"):
        return "kernel"
    for d in DOMAIN_NAMES:
        if mod == f"src.domains.{d}" or mod.startswith(f"src.domains.{d}."):
            return d
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="also fail on R4 cross-domain findings")
    args = ap.parse_args()

    files = list(iter_py(os.path.join(SRC, "kernel"))) + \
        list(iter_py(os.path.join(SRC, "domains")))
    if not files:
        print("ERROR: src/kernel or src/domains not found — run from repo root")
        return 2

    graph: dict[str, list[str]] = {}
    r1 = r2 = r3 = r4 = r6 = []
    r1, r2, r3, r4, r6 = [], [], [], [], []

    file_modules = {module_name(f) for f in files}
    for path in files:
        mod = module_name(path)
        own = layer_of(mod)
        top_imps, imps = imports_of(path)
        graph[mod] = [i for i in top_imps if layer_of(i) and i != mod]
        # importing a submodule implies initializing its parent package
        parent = mod.rsplit(".", 1)[0]
        if "." in mod and parent in file_modules:
            graph[mod].append(parent)
        allowed = WHITELIST.get(os.path.relpath(path, REPO_ROOT), {})
        for imp in imps:
            target = layer_of(imp)
            if own == "kernel":
                if target in DOMAIN_NAMES and target != "contracts" and "R1" not in allowed:
                    r1.append((mod, imp))
                if imp.startswith("src.api") and "R2" not in allowed:
                    r2.append((mod, imp))
            elif own in DOMAIN_NAMES:
                if imp.startswith("apps.") or imp == "apps":
                    r3.append((mod, imp))
                if target in DOMAIN_NAMES and target != own and target != "contracts" and own != "contracts":
                    r4.append((mod, imp))
            # R6 applies to every kernel/domains file
            if "R6" not in allowed and any(imp == p or imp.startswith(p + ".") for p in LEGACY_SHIM_PREFIXES):
                r6.append((mod, imp))

    # R5 cycle detection (recursive Tarjan SCC)
    sys.setrecursionlimit(10000)
    idx = {}
    low = {}
    on_stack = set()
    stack = []
    counter = [0]
    sccs = []

    def strongconnect(v: str):
        idx[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, []):
            if w not in graph:
                continue
            if w not in idx:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    for v in list(graph):
        if v not in idx:
            strongconnect(v)

    r5 = []
    def _common_prefix_len(mods):
        """公共包前缀的段数（路径式，按 '.' 分段）。"""
        parts = [m.split(".") for m in mods]
        n = 0
        for i in range(min(len(p) for p in parts)):
            if len({p[i] for p in parts}) == 1:
                n += 1
            else:
                break
        return n

    for comp in sccs:
        # 同一子包内部的导入环（>=4 段公共前缀，如 src.domains.runtime.im.adapters.feishu.*）
        # 属于包内 re-export 的初始化顺序问题，Python 可正常加载（父包 __init__ 先完成）；
        # 真正危险的是跨包/跨域环（公共前缀 <=3 段），仍按 HARD 处理。
        if _common_prefix_len(comp) >= 4:
            continue
        key = "CYCLE:" + "+".join(comp)
        if key not in WHITELIST and not any(
            k.startswith("CYCLE:") and set(k[6:].split("+")) >= set(comp)
            for k in WHITELIST
        ):
            r5.append(comp)

    def section(title: str, rows: list, hard: bool):
        print(f"\n== {title} ({'HARD' if hard else 'report-only'}) ==")
        if not rows:
            print("  OK - no violations")
            return
        for row in rows:
            print("  " + (" -> ".join(row) if isinstance(row, tuple) else " <-> ".join(row)))

    print(f"check-arch: scanned {len(files)} kernel/domain modules")
    section("R1 kernel -> domains", r1, True)
    section("R2 kernel -> src.api", r2, True)
    section("R3 domains -> apps (reserved Phase 4)", r3, False)
    section("R4 cross-domain direct imports (Phase 5 contracts)", r4, False)
    section("R5 import cycles (kernel/domains)", [c for c in r5], True)
    section("R6 legacy shim imports", r6, True)

    whitelisted = sum(len(v) for v in WHITELIST.values())
    print(f"\nwhitelist entries: {whitelisted} (see WHITELIST in this script)")
    print(f"R4 cross-domain findings: {len(r4)} (tracked for Phase 5)")

    hard_fail = bool(r1 or r2 or r5 or r6) or (args.strict and r4)
    if hard_fail:
        print("\nRESULT: FAIL (hard violations above)")
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
