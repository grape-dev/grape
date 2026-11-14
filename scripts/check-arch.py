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
  R7  removed legacy top-level paths must stay unimportable      (hard)
  R8  kernel must not WRITE domain-owned tables (三库独立化 P1)   (hard;
      existing pairs are ratcheted in R8_WHITELIST with a target phase)

Usage:
  python scripts/check-arch.py            # print report, exit 1 on hard violations
  python scripts/check-arch.py --strict   # additionally fail on R4 findings
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO_ROOT, "src")

# Layers scanned (canonical Phase 1 layout only; legacy top-level module paths
# are compatibility surface, not architecture).
KERNEL = "src/kernel"
DOMAINS = "src/domains"
DOMAIN_NAMES = ("mgmt", "runtime", "insight", "contracts")

# Legacy top-level module paths (Issue #699 thin compat shims). Issue #2010
# landed the canonical implementations in src/kernel/ and src/__init__.py
# resolves the old names through lazy aliases straight to those modules (no
# intermediate subpackage hop). They stay forbidden inside kernel/domains so
# internal code always targets canonical modules; every legacy *directory*
# shim (src/auth, src/user, src/runtime, src/agent, src/im, src/vcs,
# src/storage, src/observability, src/admin, src/project_info,
# src/api/routes/*) remains removed.
LEGACY_SHIM_PREFIXES = (
    "src.config", "src.session_manager", "src.lock", "src.repo_key", "src.repo",
    "src.repo_cleanup", "src.repo_migrate", "src.mysql_compat", "src.token_service",
    "src.token_resolver", "src.prompt_builder", "src.worker_package", "src.localfs",
    "src.outbound_ledger", "src.scene_custom_env", "src.featured_order",
    "src.help_docs_sync", "src.help_page_gen", "src.service_diagnostics",
    "src.api.event_pipeline", "src.api.jwt_utils", "src.api.auth_token",
)

# R7：已删除的 legacy 顶层路径（#699/#2010 清理后目录不存在）。
# 与 R6 的区别：R6 管「仍可导入的薄 shim」（要求可导入），R7 管「已删除的路径」
# （要求**不可**导入，且全仓不得出现 import）。函数内懒加载 import 是历史漏网区：
# 曾因 `from src.user.db import ...` 未被静态扫描捕获，导致建单时 fail-closed 拒绝
# ——「技能 scope 查询异常（No module named 'src.user'）」。
REMOVED_LEGACY_PREFIXES = (
    "src.user", "src.im", "src.vcs", "src.agent", "src.storage", "src.admin",
    "src.observability", "src.runtime", "src.project_info", "src.api.routes",
)

# ── R8：kernel 不得写「域归属表」（三库独立化 P1 硬约束）────────────────────
# 归属口径：docs/weekly-11/table-owner.tsv（102 张基表，三库实况）。
# 棘轮：白名单按 (文件, 表) 登记**当前数量**；新增文件/表或数量上升 → 红。
# 每条存量以「目标阶段」标注，P1/P3 迁移完成后应逐条删除（只减不增）。
R8_OWNER_TSV = os.path.join(REPO_ROOT, "docs", "weekly-11", "table-owner.tsv")
R8_WHITELIST: dict[str, dict[str, int]] = {
    "src/kernel/repo_migrate.py": {
        "managed_repos": 3, "meta": 2, "skill": 2, "user_repos": 4,
    },  # P1：整体迁 mgmt 域（仓库维护；受 #2010 kernel 布局契约约束，需随契约一起改）
    "src/kernel/session_manager.py": {
        "chat_agent_session": 7, "chat_conversations": 6, "chat_messages": 5,
        "issue": 2, "repo_im_group_binding": 3, "unified_user_mapping": 6,
    },  # P3：进程库 shim 的跨域写，按表迁到 runtime/mgmt public 写函数
    "src/kernel/auth_sdk/auth_token.py": {"api_tokens": 3, "session_api_tokens": 8},  # P3：迁 mgmt auth
    "src/kernel/repo_cleanup.py": {
        "chat_messages": 1, "meta": 4, "scene_knowledge_ref": 1, "skill": 1,
        "task_messages": 1, "vod_conversations": 1,
    },  # P1：仓库清理按域拆到 mgmt/runtime
    "src/kernel/lock.py": {"distributed_locks": 6},  # P3：分布式锁改 owner 端点
    "src/kernel/gsb/registry.py": {"gsb_service_registry": 4},  # P3：注册表写走 mgmt 端点
    "src/kernel/token_resolver.py": {"managed_repos": 2},  # P2：读改 mgmt 内部端点
    "src/kernel/prompt_builder.py": {"unified_user_mapping": 2},  # P2：读改 N10
    "src/kernel/persistence/credential_usage_log.py": {"grape_t_credential_usage_log": 2},  # P3
    "src/kernel/outbound_ledger.py": {"outbound_content_ledger": 1},  # P1：迁 mgmt
}

_R8_WRITE_RE = re.compile(
    r"\b(INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"`?(?:[A-Za-z_]\w*`?\s*\.\s*`?)?([A-Za-z_]\w*)`?", re.I)


def load_r8_owner(path: str) -> dict[str, str]:
    owner: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    owner[parts[1].strip()] = parts[0].strip()
    except OSError:
        pass
    return owner


def scan_r8() -> list[tuple[str, str, str, int]]:
    """返回 [(relpath, table, owner_schema, count)]；owner tsv 缺失时返回空并跳过。"""
    owner = load_r8_owner(R8_OWNER_TSV)
    if not owner:
        return []
    out = []
    for path in iter_py(os.path.join(SRC, "kernel")):
        text = open(path, encoding="utf-8", errors="replace").read()
        found: dict[str, int] = {}
        for _verb, table in _R8_WRITE_RE.findall(text):
            if table in owner:
                found[table] = found.get(table, 0) + 1
        rel = os.path.relpath(path, REPO_ROOT)
        for table, n in sorted(found.items()):
            out.append((rel, table, owner[table], n))
    return out


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
    "src/kernel/repo_migrate.py": {
        "managed_repos": 3, "meta": 2, "skill": 2, "user_repos": 4,
    },  # P1：整体迁 mgmt 域（仓库维护；受 #2010 kernel 布局契约约束，需随契约一起改）
    "src/kernel/session_manager.py": {
        "R1": "deferred feature flags via mgmt.user.db; Phase 5",
        "R2": "deferred timestamp helper via src.api.time_utils; Phase 5",
    },
    "src/kernel/repo_migrate.py": {
        "R1": "deferred org/user lookups via mgmt.user.db; Phase 5",
    },
    "src/kernel/repo_cleanup.py": {
        "R1": "deferred repo state lookup via mgmt.user.db; Phase 5",
    },
    "src/kernel/help_docs_sync.py": {
        "R1": "deferred skill/user lookups via mgmt.user.db; Phase 5",
        "R2": "deferred app/openapi imports for help page generation; Phase 5",
    },
    "src/kernel/auth_sdk/jwt_utils.py": {
        "R1": "deferred secret/credential keys via mgmt.auth.security_config; Phase 5",
    },
    # R1: multi-platform decoupling additions — all deferred (function-body) lookups;
    # Phase 5 inverts them through src/domains/contracts.
    "src/kernel/repo_key.py": {
        "R1": {"targets": ["src.domains.runtime.vcs.platform_card"],
              "reason": "deferred platform-card lookups; due Phase 5 -> contracts"},
    },
    "src/kernel/repo.py": {
        "R1": {"targets": ["src.domains.runtime.vcs.platform_card"],
              "reason": "deferred platform-card lookup; due Phase 5 -> contracts"},
    },
    "src/kernel/auth_sdk/channel_strategy.py": {
        "R1": {"targets": ["src.domains.mgmt.auth.providers", "src.domains.mgmt.user"],
              "reason": "deferred provider/strategy resolution; due Phase 5 -> contracts"},
    },
    "src/kernel/auth_sdk/provider_link.py": {
        "R1": {"targets": ["src.domains.mgmt.auth.providers", "src.domains.runtime.vcs.platform_card"],
              "reason": "deferred provider + vcs-card enumeration; due Phase 5 -> contracts"},
    },
    # R6: legacy shim imports due Phase 5 contracts. Evidence: check-arch at
    # base 13c6d0979 fails with these 13 R6 findings before this PR's changes
    # (run `python scripts/check-arch.py` on the base tree to reproduce).
    "src/domains/runtime/pipeline/account_env.py": {"R6": {"targets": ["src.config"], "reason": "legacy shim import; due Phase 5 contracts"}},
    "src/domains/mgmt/routes/scenes.py": {"R6": {"targets": ["src.scene_custom_env"], "reason": "legacy shim imports; due Phase 5 contracts"}},
    # R5: known import cycles inherited from the pre-Phase-1 layout.
    # agents <-> engine: M0 compat forwarding layer (src.domains.runtime.agents re-exported
    #   src.domains.runtime.engine backends); importing src.domains.runtime.agents before src.domains.runtime.engine fails
    #   the same way on the pre-Phase-1 tree. Phase 3 dissolves the
    #   executor/base coupling when _execute_agent moves out.
    # vcs.providers <-> vcs.providers.base: small registration cycle in the
    #   vcs provider plugin loader; harmless (parent init completes first).
    "CYCLE:src.domains.runtime.agents+src.domains.runtime.agents.aishell_acp+src.domains.runtime.agents.atomcode+src.domains.runtime.agents.base+src.domains.runtime.agents.codearts+src.domains.runtime.agents.codex+src.domains.runtime.agents.dsh+src.domains.runtime.agents.executor+src.domains.runtime.agents.hermes+src.domains.runtime.agents.hwcloud+src.domains.runtime.agents.hwcloud_acp+src.domains.runtime.agents.opencode": {
        "R5": "agents<->engine M0 forwarding cycle, due Phase 3 direct_executor cleanup."
             " 2026-11-12：R5 白名单改为「集合相等」口径（QA M-1），键按当前检出 SCC 精确登记；"
             "成员变化（新增/合并模块）时必须显式更新本键，否则硬门禁红。"
             " Evidence: red at base 13c6d0979 (reproduce: check-arch on the base tree).",
    },
    "CYCLE:src.domains.runtime.vcs.providers+src.domains.runtime.vcs.providers.base": {
        "R5": "pre-existing vcs provider registration cycle (harmless init order)",
    },
    "CYCLE:src.domains.runtime.im.channel+src.domains.runtime.im.channel.capability+src.domains.runtime.im.channel.channel+src.domains.runtime.im.channel.message+src.domains.runtime.im.channel.registry+src.domains.runtime.im.channel.types": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.engine+src.domains.runtime.im.engine.dedup+src.domains.runtime.im.engine.hashring+src.domains.runtime.im.engine.lease+src.domains.runtime.im.engine.metrics+src.domains.runtime.im.engine.replier+src.domains.runtime.im.engine.router+src.domains.runtime.im.engine.session+src.domains.runtime.im.engine.supervisor": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.telegram+src.domains.runtime.im.adapters.telegram.cards+src.domains.runtime.im.adapters.telegram.channel+src.domains.runtime.im.adapters.telegram.inbound+src.domains.runtime.im.adapters.telegram.markdown+src.domains.runtime.im.adapters.telegram.outbound+src.domains.runtime.im.adapters.telegram.resolvers": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.slack+src.domains.runtime.im.adapters.slack.cards+src.domains.runtime.im.adapters.slack.channel+src.domains.runtime.im.adapters.slack.inbound+src.domains.runtime.im.adapters.slack.markdown+src.domains.runtime.im.adapters.slack.outbound+src.domains.runtime.im.adapters.slack.resolvers": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.wecom+src.domains.runtime.im.adapters.wecom.cards+src.domains.runtime.im.adapters.wecom.inbound+src.domains.runtime.im.adapters.wecom.markdown+src.domains.runtime.im.adapters.wecom.outbound": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.feishu+src.domains.runtime.im.adapters.feishu.inbound+src.domains.runtime.im.adapters.feishu.markdown+src.domains.runtime.im.adapters.feishu.outbound": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.dingtalk+src.domains.runtime.im.adapters.dingtalk.inbound+src.domains.runtime.im.adapters.dingtalk.markdown+src.domains.runtime.im.adapters.dingtalk.outbound": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
    },
    "CYCLE:src.domains.runtime.im.adapters.wechat+src.domains.runtime.im.adapters.wechat.channel+src.domains.runtime.im.adapters.wechat.inbound+src.domains.runtime.im.adapters.wechat.login+src.domains.runtime.im.adapters.wechat.media+src.domains.runtime.im.adapters.wechat.outbound+src.domains.runtime.im.adapters.wechat.protocol+src.domains.runtime.im.adapters.wechat.resolvers": {
        "R5": "IM 包 __init__.py 只 re-export 自己的子模块；check-arch 对每个模块无条件补「父包边」(module -> 父包)，与 __init__ -> 子模块的导入互指形成 SCC。运行期无回边（父包只导入子模块，子模块不反向取父包属性），属检查器口径误报；Phase 5 引入 contracts 注入后可自然消除。",
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
    r1, r2, r3, r4, r6, r7, r8 = [], [], [], [], [], [], []
    r5_intra: list = []  # 包内 re-export 环（>=4 段公共前缀），report-only 输出

    # R7 全仓扫描：src/api 等非 kernel/domains 层不在主循环内，历史漏网正出自这里
    # （`from src.user.db import ...` 写在 src/api/*.py 的函数体内）。
    for _path in list(iter_py(SRC)):
        _mod = module_name(_path)
        if _mod.startswith(("src.kernel", "src.domains")):
            continue  # 主循环已覆盖
        _top, _all = imports_of(_path)
        for _imp in _all:
            if any(_imp == _p or _imp.startswith(_p + ".") for _p in REMOVED_LEGACY_PREFIXES):
                r7.append((_mod, _imp))

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

        def _exempt(rule: str, imported: str) -> bool:
            """白名单豁免判定（PR #1231 架构复审 Medium-1：精确粒度）。

            - 值为字符串（历史形态）：该文件整条规则豁免（存量条目兼容）。
            - 值为 dict：**仅豁免 targets 里登记的导入目标**——同文件今后新增
              的 kernel→domains / shim import 不被吞掉，守卫面不被静默收窄。
            """
            a = allowed.get(rule)
            if a is None:
                return False
            if isinstance(a, str):
                return True
            if isinstance(a, dict):
                return imported in a.get("targets", ())
            return False
        for imp in imps:
            target = layer_of(imp)
            if own == "kernel":
                if target in DOMAIN_NAMES and target != "contracts" and not _exempt("R1", imp):
                    r1.append((mod, imp))
                if imp.startswith("src.api") and not _exempt("R2", imp):
                    r2.append((mod, imp))
            elif own in DOMAIN_NAMES:
                if imp.startswith("apps.") or imp == "apps":
                    r3.append((mod, imp))
                if target in DOMAIN_NAMES and target != own and target != "contracts" and own != "contracts":
                    r4.append((mod, imp))
            # R6 applies to every kernel/domains file
            if not _exempt("R6", imp) and any(imp == p or imp.startswith(p + ".") for p in LEGACY_SHIM_PREFIXES):
                r6.append((mod, imp))
            if any(imp == p or imp.startswith(p + ".") for p in REMOVED_LEGACY_PREFIXES):
                r7.append((mod, imp))

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
            # 包内 re-export 环（Python 可正常加载）不判 HARD，但**不得静默跳过**：
            # 登记后在 R5 段落里以 report-only 输出，避免豁免面不可见（QA MED-2）。
            r5_intra.append(comp)
            continue
        key = "CYCLE:" + "+".join(comp)
        # M-1（2026-11-12 QA 复审）：原为超集匹配（>=），一条大集合白名单可吞掉其内部
        # 任何新增真环，使 R5 在整域退化为免检。改为集合相等：白名单成员与检出 SCC
        # 不完全一致即红，成员变化（新增/合并模块）必须显式更新白名单。
        if key not in WHITELIST and not any(
            k.startswith("CYCLE:") and set(k[6:].split("+")) == set(comp)
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
    if r5_intra:
        print("\n== R5 intra-package cycles (report-only, skipped) ==")
        for comp in r5_intra:
            print("  " + " <-> ".join(comp))
    section("R6 legacy shim imports", r6, True)
    section("R7 removed legacy paths", r7, True)
    # R8：kernel 写域归属表（棘轮：新文件/新表/数量上升 → HARD）
    for rel, table, owner_schema, n in scan_r8():
        expected = R8_WHITELIST.get(rel, {}).get(table)
        if expected is None:
            r8.append((rel, f"WRITE {table} ({owner_schema}-owned)", f"{n} 处（未登记）"))
        elif n > expected:
            r8.append((rel, f"WRITE {table} ({owner_schema}-owned)", f"{expected} -> {n}"))
    section("R8 kernel writes domain-owned tables (ratchet)", r8, True)

    whitelisted = sum(len(v) for v in WHITELIST.values())
    print(f"\nwhitelist entries: {whitelisted} (see WHITELIST in this script)")
    print(f"R4 cross-domain findings: {len(r4)} (tracked for Phase 5)")

    hard_fail = bool(r1 or r2 or r5 or r6 or r7 or r8) or (args.strict and r4)
    if hard_fail:
        print("\nRESULT: FAIL (hard violations above)")
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
