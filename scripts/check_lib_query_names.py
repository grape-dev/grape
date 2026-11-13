#!/usr/bin/env python3
"""A3 门禁：跨域读**查询名** ⊆ 目标域 lib 白名单（可执行、可门禁）。

**为什么需要本门禁**

lib 模式只把**登记过的查询面**暴露给跨域调用方：

- `read_one(name)` / `read_all(name)` 走 `_READ_QUERIES` 命名查询表，未登记的名字
  只会在运行时 `KeyError`；
- 其余函数是 `src.domains.<domain>.public` 的公开可调用对象，未登记即 `AttributeError`。

这类漂移（改名、漏登记、手抖写错名）只有在 `lib is not None` 分支真正执行时才炸。
而单机拓扑下 lib 开关默认关闭，`mgmt_lib()` 直接返回 None，**整段跨域读代码在测试里
根本不会跑**，于是问题留到三库三机切换那一刻才第一次暴露——正是设计文档 §7
「视图退役遗漏读点 → 功能静默失效」的风险。

本脚本用 AST 静态比对，把「引用了哪些查询名」与「登记了哪些查询名」变成可断言、
可门禁的检查（验收矩阵 A3：`used_names - registry_names == ∅`）。

**判定口径**

| 引用形态 | 必须命中的注册表 |
|---|---|
| `lib.read_one("<name>", ...)` / `lib.read_all("<name>", ...)` | 目标域 public 模块的 `_READ_QUERIES` 键 |
| `lib.<其它函数>(...)` / `lib.<属性>` | 目标域 public 模块的非下划线可调用对象（`available` 为探活口，不计） |

- 作用域按函数各自独立解析：同一文件里不同函数可以对同名变量赋不同域的句柄。
- 查询名不是字符串字面量（拼接/变量）→ 记 `dynamic`，静态无法证明闭合，判 FAIL
  （A3 要求闭包，动态名意味着白名单约束在这条路径上失效）。

**用法**

    python3 scripts/check_lib_query_names.py                # 扫 src/
    python3 scripts/check_lib_query_names.py src tests      # 指定路径
    python3 scripts/check_lib_query_names.py --json         # 机器可读（CI 归档）

退出码：0 = 闭合；1 = 存在未登记查询名/无法静态证明；2 = fail-closed（没扫到任何文件）。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: domain_read 暴露的入口函数 → 目标域
LIB_FUNCS: dict[str, str] = {
    "mgmt_lib": "mgmt",
    "runtime_lib": "runtime",
    "insight_lib": "insight",
}

#: `from src.kernel.domain_read import ...` 的模块后缀（容忍 src. 前缀的写法差异）
IMPORT_SUFFIX = "kernel.domain_read"

#: 走命名查询表的方法（第一个位置参数就是查询名）
NAMED_QUERY_ATTRS: frozenset[str] = frozenset({"read_one", "read_all"})

#: 命名查询表在 public 模块里的变量名
QUERY_REGISTRY_ATTR = "_READ_QUERIES"

#: 探活函数不是查询名，不参与 A3 白名单比对
PROBE_NAMES = {"available"}

_SKIP_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

KIND_ATTR = "attr"
KIND_QUERY = "query"
KIND_DYNAMIC = "dynamic_query"


@dataclass(frozen=True)
class LibUsage:
    """一处跨域读查询面引用。"""

    file: str
    line: int
    domain: str
    kind: str
    name: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "domain": self.domain,
                "kind": self.kind, "name": self.name}


@dataclass
class DomainRegistry:
    """目标域 lib 的注册面。"""

    functions: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)


@dataclass
class DomainReport:
    """单个域的闭合性结论。"""

    domain: str
    names: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)
    dynamic: list[LibUsage] = field(default_factory=list)
    registry: DomainRegistry | None = None

    @property
    def unregistered_names(self) -> set[str]:
        if self.registry is None:
            return set(self.names)
        return self.names - self.registry.functions

    @property
    def unregistered_queries(self) -> set[str]:
        if self.registry is None:
            return set(self.queries)
        return self.queries - self.registry.queries

    @property
    def ok(self) -> bool:
        return (self.registry is not None
                and not self.unregistered_names
                and not self.unregistered_queries
                and not self.dynamic)


def _scope_nodes(body: Sequence[ast.stmt]):
    """遍历一个作用域的节点，**不进入**嵌套函数/类/lambda（各自算独立作用域）。"""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SKIP_NODES):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SKIP_NODES):
                continue
            stack.append(child)


def _iter_scope_bodies(tree: ast.AST):
    """产出模块级与每个函数级作用域的语句列表。"""
    yield getattr(tree, "body", [])
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.body


def _lib_aliases(tree: ast.AST) -> dict[str, str]:
    """收集 `domain_read` 入口函数的本地别名 → 域。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").endswith(IMPORT_SUFFIX):
            continue
        for alias in node.names:
            domain = LIB_FUNCS.get(alias.name)
            if domain:
                aliases[alias.asname or alias.name] = domain
    return aliases


def _is_lib_call(node: ast.AST | None, aliases: dict[str, str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in aliases
    )


def _handle_vars(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> dict[str, str]:
    """一个作用域内 `x = <domain>_lib(...)` 的句柄变量 → 域。"""
    handles: dict[str, str] = {}
    for node in nodes:
        targets: Sequence[ast.AST] = ()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = (node.target,), node.value
        if not _is_lib_call(value, aliases):
            continue
        domain = aliases[value.func.id]  # type: ignore[union-attr]
        for target in targets:
            if isinstance(target, ast.Name):
                handles[target.id] = domain
    return handles


def _resolve_lib_expr(node: ast.AST, handles: dict[str, str], aliases: dict[str, str]) -> str | None:
    """表达式是否来自某个域的 lib 句柄；是则返回域名。"""
    if isinstance(node, ast.Name) and node.id in handles:
        return handles[node.id]
    if _is_lib_call(node, aliases):
        return aliases[node.func.id]  # type: ignore[union-attr]
    return None


def collect_usage(source: str, filename: str) -> list[LibUsage]:
    """从一份源码里收集所有跨域读查询面引用（去重，按行序）。"""
    tree = ast.parse(source, filename=filename)
    aliases = _lib_aliases(tree)
    if not aliases:
        return []

    seen: set[tuple[int, str, str]] = set()
    usage: list[LibUsage] = []

    def _add(u: LibUsage) -> None:
        key = (u.line, u.kind, u.name)
        if key in seen:
            return
        seen.add(key)
        usage.append(u)

    for scope_body in _iter_scope_bodies(tree):
        nodes = list(_scope_nodes(scope_body))
        if not nodes:
            continue
        handles = _handle_vars(nodes, aliases)

        # 先标出「作为命名查询调用入口」的 Attribute，避免重复计成普通函数引用
        named_query_funcs: set[int] = set()
        for node in nodes:
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in NAMED_QUERY_ATTRS
                    and _resolve_lib_expr(node.func.value, handles, aliases)):
                named_query_funcs.add(id(node.func))
                domain = _resolve_lib_expr(node.func.value, handles, aliases)
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    _add(LibUsage(filename, node.lineno, domain or "", KIND_QUERY, first.value))
                else:
                    _add(LibUsage(filename, node.lineno, domain or "", KIND_DYNAMIC))

        for node in nodes:
            if not isinstance(node, ast.Attribute) or id(node) in named_query_funcs:
                continue
            domain = _resolve_lib_expr(node.value, handles, aliases)
            if domain:
                _add(LibUsage(filename, node.lineno, domain, KIND_ATTR, node.attr))

    return sorted(usage, key=lambda u: (u.line, u.kind, u.name))


def collect_registry(domain: str) -> DomainRegistry | None:
    """目标域 lib 的注册面；模块不存在 → None（该域尚无 lib 面）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        module = importlib.import_module(f"src.domains.{domain}.public")
    except ModuleNotFoundError:
        return None
    functions = {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and callable(value)
    } - PROBE_NAMES
    queries = getattr(module, QUERY_REGISTRY_ATTR, None)
    return DomainRegistry(
        functions=functions,
        queries=set(queries) if isinstance(queries, dict) else set(),
    )


def iter_source_files(paths: list[Path]):
    """展开待扫文件（目录递归，跳过缓存与虚拟环境）。"""
    for path in paths:
        if path.is_file():
            if path.suffix == ".py":
                yield path
            continue
        for child in sorted(path.rglob("*.py")):
            parts = set(child.parts)
            if "__pycache__" in parts or {".venv", "venv", "node_modules"} & parts:
                continue
            yield child


def check(paths: list[Path],
          *,
          registry: dict[str, DomainRegistry | None] | None = None) -> list[DomainReport]:
    """对给定路径做 A3 闭合性检查。`registry` 可注入（测试用），默认从模块导入。"""
    files = list(iter_source_files(paths))
    injected = registry is not None
    regs: dict[str, DomainRegistry | None] = dict(registry or {})

    reports: dict[str, DomainReport] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(path)
        for usage in collect_usage(source, rel):
            report = reports.setdefault(usage.domain, DomainReport(usage.domain))
            if usage.kind == KIND_ATTR:
                report.names.add(usage.name)
            elif usage.kind == KIND_QUERY:
                report.queries.add(usage.name)
            else:
                report.dynamic.append(usage)

    for domain, report in reports.items():
        if not injected or domain not in regs:
            regs[domain] = collect_registry(domain)
        report.registry = regs.get(domain)

    return sorted(reports.values(), key=lambda r: r.domain)


def _render(reports: list[DomainReport], files_scanned: int) -> str:
    lines = [">>> lib query-name closure (A3)", f"    files scanned: {files_scanned}"]
    for report in reports:
        if report.registry is None:
            lines.append(f"    {report.domain}: {len(report.names) + len(report.queries)} used / "
                         f"registry missing → {'FAIL' if (report.names or report.queries) else 'N/A'}")
        else:
            verdict = "OK" if report.ok else "FAIL"
            lines.append(
                f"    {report.domain}: {len(report.names)} funcs + {len(report.queries)} named queries used / "
                f"{len(report.registry.functions)} funcs + {len(report.registry.queries)} queries registered → {verdict}"
            )
        for name in sorted(report.unregistered_names):
            lines.append(f"      - unregistered function/attr: {name}")
        for name in sorted(report.unregistered_queries):
            lines.append(f"      - unregistered named query: {name}")
        for usage in report.dynamic:
            lines.append(f"      - dynamic query name (unverifiable): {usage.file}:{usage.line}")
    failed = [r for r in reports if not r.ok]
    lines.append(">>> PASS: 全部查询名已登记" if not failed else f">>> FAIL: {len(failed)} 个域未闭合")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A3 gate: lib query names must be registered")
    parser.add_argument("paths", nargs="*", default=["src"], help="待扫路径（默认 src/）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    argv_paths = args.paths or ["src"]
    targets = [p if p.is_absolute() else (REPO_ROOT / p) for p in map(Path, argv_paths)]
    files = list(iter_source_files(targets))
    if not files:
        print(f">>> FAIL: fail-closed，未扫到任何 .py 文件（{', '.join(argv_paths)}）")
        return 2

    reports = check(targets)
    if args.json:
        print(json.dumps({
            "files_scanned": len(files),
            "reports": [
                {
                    "domain": r.domain,
                    "functions_used": sorted(r.names),
                    "named_queries_used": sorted(r.queries),
                    "registry_present": r.registry is not None,
                    "unregistered_functions": sorted(r.unregistered_names),
                    "unregistered_queries": sorted(r.unregistered_queries),
                    "dynamic_queries": [u.as_dict() for u in r.dynamic],
                    "ok": r.ok,
                }
                for r in reports
            ],
            "ok": all(r.ok for r in reports) if reports else True,
        }, ensure_ascii=False, indent=2))
    else:
        print(_render(reports, len(files)))
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())