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

**句柄来源（三者等价，均识别为跨域读句柄）**

| 形态 | 说明 |
|---|---|
| `from src.kernel.domain_read import <domain>_lib` + `x = <domain>_lib(...)` | 显式句柄（lib 入口函数） |
| `from src.domains.<domain> import public as x` + `x.<func>` | 直连 public 模块的**隐式句柄**（`GRAPE_*_VIA_LIB` 开关分支的写法） |
| `x = <同文件工厂>()`，工厂体 `return <隐式句柄>[ if ... else None]` | 工厂返回的句柄（单跳；如 `_creds_via_lib()` / `_task_via_lib()`） |

- 作用域按函数各自独立解析：同一文件里不同函数可以对同名变量赋不同域的句柄；
  隐式句柄只在声明它的作用域内可见（函数内 `import` 不外泄，本地赋值/循环变量会遮蔽继承来的别名）。
- 查询名不是字符串字面量（拼接/变量）→ 记 `dynamic`，静态无法证明闭合，判 FAIL
  （A3 要求闭包，动态名意味着白名单约束在这条路径上失效）。
- 探活口 `available`（及注册表侧扣除）在使用侧同样跳过：`x.available()` 不是查询名引用。

**已知不覆盖的写法（静态盲区，靠 review 兜底；不在本门禁的证明范围）**

- `x := <domain>_lib(...)` 海象赋值产生的句柄（`_handle_vars` 只认 `Assign`/`AnnAssign`）；
  海象赋值**声明**的同名变量也不参与遮蔽判断（`_local_targets` 只收 `Assign`/`AnnAssign`/`For`/`With`）；
- `lambda` 体内部对句柄的引用（`_scope_nodes` 不进入 `Lambda`/嵌套函数/类）；
- 同行多域同名引用在 `LibUsage` 去重键上的历史形态（已按 `line+kind+name+domain` 去重，
  同一行不同域不再互相吞掉，但同名同域的重复引用只记一次）；
- 跨文件的句柄传递（`f(lib)` 形参、返回值再赋值超过一跳）；
- `import src.domains.<domain>.public as x` / `from src.domains.<domain>.public import <func>`
  两种等价写法（本仓库无此形态，出现时需扩展 `_module_aliases`）。

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


def _lib_aliases(tree: ast.AST) -> tuple[dict[str, str], bool]:
    """收集 `domain_read` 入口函数的本地别名 → 域；并报告是否存在直连 public 模块的 import。"""
    aliases: dict[str, str] = {}
    has_public = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if (node.module or "").endswith(IMPORT_SUFFIX):
            for alias in node.names:
                domain = LIB_FUNCS.get(alias.name)
                if domain:
                    aliases[alias.asname or alias.name] = domain
        elif _public_module_domain(node):
            has_public = True
    return aliases, has_public


def _public_module_domain(node: ast.ImportFrom) -> str | None:
    """`from src.domains.<domain> import public` 的域名；其它形态 → None。"""
    module = node.module or ""
    bare = module[len("src."):] if module.startswith("src.") else module
    if not bare.startswith("domains."):
        return None
    parts = bare.split(".")
    # domains.<domain> + import public [as X]
    if len(parts) == 2 and parts[1] and any(a.name == "public" for a in node.names):
        return parts[1]
    return None


def _module_aliases_from(node: ast.ImportFrom) -> dict[str, str]:
    """单个 import 语句引入的 public 模块别名（`as X` 或裸 `public`）→ 域。"""
    domain = _public_module_domain(node)
    if domain is None:
        return {}
    return {(a.asname or a.name): domain for a in node.names if a.name == "public"}


def _module_aliases(nodes: Sequence[ast.AST]) -> dict[str, str]:
    """一个作用域内 `from src.domains.<domain> import public as X` 的别名 → 域。

    直连 public 模块拿到的是**隐式句柄**：`X.<func>` 与 `<domain>_lib().<func>` 等价，
    A3 口径同样要求其引用名 ⊆ 注册表。
    """
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            aliases.update(_module_aliases_from(node))
    return aliases


def _local_targets(nodes: Sequence[ast.AST]) -> set[str]:
    """作用域内被本地赋值/循环变量/`with ... as x` 绑定的名字（同名会遮蔽继承来的别名）。"""
    targets: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign):
            bound: Sequence[ast.AST | None] = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            bound = (node.target,)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound = (node.target,)
        elif isinstance(node, ast.With):
            bound = [item.optional_vars for item in node.items]
        else:
            continue
        for target in bound:
            stack: list[ast.AST] = [target] if target is not None else []
            while stack:
                current = stack.pop()
                if isinstance(current, ast.Name):
                    targets.add(current.id)
                elif isinstance(current, (ast.Tuple, ast.List)):
                    stack.extend(current.elts)
                elif isinstance(current, ast.Starred):
                    stack.append(current.value)
    return targets


def _alias_only_domain(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    """表达式是否**直接**是某个域句柄（含 `x if cond else None` 三元分支）。"""
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id)
    if isinstance(expr, ast.IfExp):
        return _alias_only_domain(expr.body, aliases) or _alias_only_domain(expr.orelse, aliases)
    return None


def _scope_aliases(nodes: Sequence[ast.AST], inherited: dict[str, str]) -> dict[str, str]:
    """本作用域可见的隐式句柄：本作用域内 import 的 + 模块级继承（被本地绑定遮蔽的除外）。"""
    aliases = {**inherited, **_module_aliases(nodes)}
    if not aliases:
        return {}
    shadowed = _local_targets(nodes)
    return {name: domain for name, domain in aliases.items() if name not in shadowed}


def _factory_domains(module_nodes: Sequence[ast.AST],
                     module_aliases: dict[str, str]) -> dict[str, str]:
    """同文件里 `def f(): ... return <隐式句柄>[ if ... else None]` 的工厂 → 域（单跳）。"""
    factories: dict[str, str] = {}
    for node in module_nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_scope_nodes(node.body))
        scope = _scope_aliases(nodes, module_aliases)
        if not scope:
            continue
        for stmt in nodes:
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            domain = _alias_only_domain(stmt.value, scope)
            if domain:
                factories[node.name] = domain
                break
    return factories


def _call_domain(node: ast.AST | None, aliases: dict[str, str],
                 factories: dict[str, str] | None) -> str | None:
    """`f(...)` 的返回是否为某域句柄：lib 入口函数，或同文件单跳工厂。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id in aliases:
        return aliases[node.func.id]
    return (factories or {}).get(node.func.id)


def _handle_vars(nodes: Sequence[ast.AST], aliases: dict[str, str],
                 factories: dict[str, str] | None = None) -> dict[str, str]:
    """一个作用域内 `x = <domain>_lib(...)` / `x = <工厂>()` 的句柄变量 → 域。"""
    handles: dict[str, str] = {}
    for node in nodes:
        targets: Sequence[ast.AST] = ()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = (node.target,), node.value
        domain = _call_domain(value, aliases, factories)
        if not domain:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                handles[target.id] = domain
    return handles


def _resolve_lib_expr(node: ast.AST, handles: dict[str, str], aliases: dict[str, str],
                      factories: dict[str, str] | None = None) -> str | None:
    """表达式是否来自某个域的 lib 句柄（变量 / 隐式模块别名 / 入口函数 / 工厂）；是则返回域名。"""
    if isinstance(node, ast.Name):
        if node.id in handles:
            return handles[node.id]
        return aliases.get(node.id)
    return _call_domain(node, aliases, factories)


def collect_usage(source: str, filename: str) -> list[LibUsage]:
    """从一份源码里收集所有跨域读查询面引用（去重，按行序）。"""
    tree = ast.parse(source, filename=filename)
    lib_aliases, has_public_import = _lib_aliases(tree)
    if not lib_aliases and not has_public_import:
        return []
    module_nodes = list(_scope_nodes(getattr(tree, "body", [])))
    module_aliases = _scope_aliases(module_nodes, {})
    factories = (_factory_domains(getattr(tree, "body", []), module_aliases)
                 if has_public_import else {})

    seen: set[tuple[int, str, str, str]] = set()
    usage: list[LibUsage] = []

    def _add(u: LibUsage) -> None:
        key = (u.line, u.kind, u.name, u.domain)
        if key in seen:
            return
        seen.add(key)
        usage.append(u)

    for scope_body in _iter_scope_bodies(tree):
        nodes = list(_scope_nodes(scope_body))
        if not nodes:
            continue
        aliases = {**lib_aliases, **_scope_aliases(nodes, module_aliases)}
        handles = _handle_vars(nodes, aliases, factories)

        # 先标出「作为命名查询调用入口」的 Attribute，避免重复计成普通函数引用
        named_query_funcs: set[int] = set()
        for node in nodes:
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in NAMED_QUERY_ATTRS
                    and _resolve_lib_expr(node.func.value, handles, aliases, factories)):
                named_query_funcs.add(id(node.func))
                domain = _resolve_lib_expr(node.func.value, handles, aliases, factories)
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    _add(LibUsage(filename, node.lineno, domain or "", KIND_QUERY, first.value))
                else:
                    _add(LibUsage(filename, node.lineno, domain or "", KIND_DYNAMIC))

        for node in nodes:
            if not isinstance(node, ast.Attribute) or id(node) in named_query_funcs:
                continue
            if node.attr in PROBE_NAMES:
                continue
            domain = _resolve_lib_expr(node.value, handles, aliases, factories)
            if domain:
                _add(LibUsage(filename, node.lineno, domain, KIND_ATTR, node.attr))

    return sorted(usage, key=lambda u: (u.line, u.kind, u.name, u.domain))


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
    # PEP 562 懒导出类（如 runtime.public 的 `_LAZY_CLASS_ATTRS`）：名字不在模块
    # vars 中，但显式登记在懒导出表里，属于公开契约面。门禁必须承认这份登记，
    # 否则 `runtime_public.VcsApiClient` 一类跨域引用会被误判为「未登记名字」。
    _lazy_attrs = getattr(module, "_LAZY_CLASS_ATTRS", None)
    if isinstance(_lazy_attrs, dict):
        functions |= {str(name) for name in _lazy_attrs if not str(name).startswith("_")}
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