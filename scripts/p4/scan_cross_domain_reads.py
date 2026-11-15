#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B0 库内重建：跨域读清单扫描器（Issue #2504）。

**为什么需要本脚本**

`docs/lib-3db-3host-design.md` §2.2 的读欠账口径（114 处/20 表 = 104 裸读 +
10 兜底噪声）由运行机本地脚本 `p4_read_converter.py` 产出，该脚本不在仓内——
A2/B2 的出口条件在仓库内不可复现。本脚本在库内等价重建「读点清单」：AST 遍历
`src/`，按「函数内是否持有对应域 lib 句柄」判定 converted / bare，输出按表分组的
清单（Markdown + JSON），并区分 auto / manual（动态列名 / 动态 IN / 非 fetch 消费）。

**判定口径（与 §2.2 的对照逐条登记在 docs/lib-3db/read-inventory-diff.md）**

| 概念 | 判据 |
|---|---|
| 读点（site） | `<cursor>.execute(<SQL>, ...)` / `executemany` 调用，SQL 以 SELECT/WITH 开头，且引用 ≥1 张「表归属清单（configs/table_ownership.json）」中归属 ≠ 宿主域 的表 |
| 宿主域（host） | `src/domains/<d>/` → 桶=域=<d>（进程归属固定，与 §2.2 的 W1 裸读分布可比）；`src/api/`、`src/kernel/` 与 `src/domains/contracts/`（GSB provider 本地实现，多进程注册加载）被多进程同时加载（mgmt/runtime 进程都 import）→ 域未知、桶单列 `api`/`kernel`，任一注册表都算跨域（视图退役后在至少一个进程属跨域），stats 里单列 shared_scope 不并入 W1 口径；其余前缀（scripts/、tests/、src 顶层）显式跳过不入清单 |
| 外域表 | SQL 文本 FROM/JOIN 语法位上出现的、在 `configs/table_ownership.json` 有归属的表名（复用 `src/kernel/db.py` 的表名语法位口径）；字符串以 f-string/拼接静态部分为准 |
| converted（已接线） | 外域表所属域 d 在宿主函数内有 lib 句柄（`x = <d>_lib()` / `from src.domains.d import public as x` / 同文件工厂），**且**本 execute 位于该句柄 guard 的兜底分支（`if 句柄 is not None: ... else: <execute>` 的 else，或 `if 句柄 is None: <execute>` 的 body）——兜底 SQL 属回滚通道，非欠账 |
| bare（裸读，真欠账） | 其余读点：无句柄，或句柄存在但 execute 不在 guard 兜底分支（lib 分支盖不住这条 SQL） |
| auto | SQL 全静态 + 紧随语句为 `X = <同游标>.fetchall()/fetchone()` 赋值——可机械改写为 read_one/read_all |
| manual-动态列名 | SQL 动态部分出现在 SELECT 列表段（FROM 之前） |
| manual-动态 IN | SQL 动态部分出现在 `IN (` 之后（占位符列表拼接） |
| manual-动态其他 | 其余动态 SQL（表名/排序/条件动态） |
| manual-非 fetch 消费 | SQL 静态但消费形态非「fetch 赋值」：`for r in c.fetchall()`、`c.fetchone()` 内联表达式、聚合/子查询消费等 |

优先级：动态 > 非 fetch > auto（一处读点只归一类）。

**用法**

    python3 scripts/p4/scan_cross_domain_reads.py                 # Markdown 清单（按表分组）
    python3 scripts/p4/scan_cross_domain_reads.py --json          # 机器可读 JSON（CI 归档）
    python3 scripts/p4/scan_cross_domain_reads.py --check         # 对照基线快照（check-all 门禁）
    python3 scripts/p4/scan_cross_domain_reads.py --table skill    # 单表查询（B2 按表收口底单）
    python3 scripts/p4/scan_cross_domain_reads.py src tests        # 指定路径

退出码：0 = 清单产出（--check 时与基线一致）；1 = --check 与基线不一致（回归）；
2 = fail-closed（没扫到文件 / 表归属清单缺失 / 未知表名 / 互斥旗标组合）。
互斥：--check / --update-baseline / --table 至多其一（过滤视图不得喂门禁/基线）；
--json 不与 --update-baseline 同用（更新基线为静默维护流程）。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNERSHIP_MANIFEST = REPO_ROOT / "configs" / "table_ownership.json"
BASELINE = REPO_ROOT / "scripts" / "p4" / "read_inventory_baseline.json"

#: domain_read 暴露的入口函数 → 目标域
LIB_FUNCS: dict[str, str] = {
    "mgmt_lib": "mgmt",
    "runtime_lib": "runtime",
    "insight_lib": "insight",
}

#: 命名查询方法（消费形态判定时不算裸 SQL 调用面）
NAMED_QUERY_ATTRS = frozenset({"read_one", "read_all"})

#: 表名语法位：FROM / JOIN 之后（与 src/kernel/db.py 的改写口径同源）
_TABLE_POS_RE = re.compile(r"(?i)\b(FROM|JOIN)(\s+)([`\"\[]?)(\w+)([`\"\]]?)")

_SKIP_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

#: 分类常量
CLS_AUTO = "auto"
CLS_DYN_COLUMN = "manual_dynamic_column"
CLS_DYN_IN = "manual_dynamic_in"
CLS_DYN_OTHER = "manual_dynamic_other"
CLS_NON_FETCH = "manual_non_fetch"

#: 分类输出顺序（stats 的 bare_classification_by_table 列序 = 附录 §5 底单列序；
#: 模块级单点维护，B1 起与测试 rows dict 共用此口径——QA 复审判断项 2 收口）
CLS_ORDER: tuple[str, ...] = (CLS_AUTO, CLS_DYN_COLUMN, CLS_DYN_IN,
                              CLS_DYN_OTHER, CLS_NON_FETCH)

_STATUS_CONVERTED = "converted"
_STATUS_BARE = "bare"


@dataclass
class ReadSite:
    """一处跨域读点。"""

    file: str
    line: int
    func: str
    host: str            # 桶标签：mgmt / runtime / insight / api / kernel
    status: str          # converted / bare
    classification: str  # auto / manual_*
    tables: list[str]   # 外域表（排序去重）
    dynamic: bool        # SQL 是否含动态拼接
    sql: str             # 提取到的静态 SQL 片段（截断）

    def as_dict(self) -> dict:
        return {
            "file": self.file, "line": self.line, "func": self.func,
            "host": self.host, "status": self.status,
            "classification": self.classification,
            "tables": self.tables, "dynamic": self.dynamic, "sql": self.sql,
        }


@dataclass
class Inventory:
    """一次扫描的清单。"""

    files_scanned: int = 0
    sites: list[ReadSite] = field(default_factory=list)

    # ---- 统计口径（全部从 sites 派生，不单独维护） ----
    def count(self, status: str | None = None) -> int:
        return sum(1 for s in self.sites if status is None or s.status == status)

    @property
    def bare(self) -> list[ReadSite]:
        return [s for s in self.sites if s.status == _STATUS_BARE]

    @property
    def converted(self) -> list[ReadSite]:
        return [s for s in self.sites if s.status == _STATUS_CONVERTED]

    def by_table(self, status: str | None = None) -> dict[str, list[ReadSite]]:
        out: dict[str, list[ReadSite]] = {}
        for s in sorted(self.sites, key=lambda x: (x.file, x.line)):
            if status is not None and s.status != status:
                continue
            for t in s.tables:
                out.setdefault(t, []).append(s)
        return out

    #: 进程归属固定的域桶（§2.2 W1 口径）；api/kernel 为跨进程共享代码，单列
    DOMAIN_HOSTS = ("mgmt", "runtime", "insight")

    @staticmethod
    def _bare_by_table_cls(w1_bare: list[ReadSite]) -> dict[str, Counter]:
        """W1 裸读按表 × 分类计数（B1 重分类派生统计的唯一构造点——
        stats 与测试共用的口径单点；QA 复审判断项 1 收口）。"""
        out: dict[str, Counter] = {}
        for s in w1_bare:
            for tb in s.tables:
                out.setdefault(tb, Counter())[s.classification] += 1
        return out

    def stats(self) -> dict:
        """清单统计。

        两层口径：
        - 全量（total/bare/converted）：src/ 全部跨域读点；
        - w1_scope：仅进程归属固定的 src/domains/<d> 桶（与 §2.2 的
          「裸读宿主分布 insight/mgmt/runtime/api」可比；api/kernel 为
          跨进程共享代码，进程归属不定，§2.2 原口径未纳入，单列 shared_*）。
        """
        bare_by_host = Counter(s.host for s in self.bare)
        cls_counter = Counter(s.classification for s in self.sites)
        w1 = [s for s in self.sites if s.host in self.DOMAIN_HOSTS]
        w1_bare = [s for s in w1 if s.status == _STATUS_BARE]
        shared = [s for s in self.sites if s.host not in self.DOMAIN_HOSTS]
        # B1 重分类：W1 裸读（真欠账）的 auto/manual 真实数字（逐表可查）
        w1_bare_cls = Counter(s.classification for s in w1_bare)
        w1_bare_by_cls = self._bare_by_table_cls(w1_bare)
        return {
            "total": len(self.sites),
            "bare": len(self.bare),
            "converted": len(self.converted),
            "bare_by_host": dict(sorted(bare_by_host.items())),
            "w1_scope": {
                "bare": len(w1_bare),
                # B6（#2504）：insight/runtime 域 W1 裸读整域清零后仍固定输出
                # 三域键（0 计），保证差异报告 §2.1 行绑定与
                # test_w1_scope_present 域桶不变式稳定。
                "bare_by_host": dict(sorted(
                    {**{h: 0 for h in self.DOMAIN_HOSTS},
                     **Counter(s.host for s in w1_bare)}.items())),
                "converted": len(w1) - len(w1_bare),
                "by_file_bare": dict(sorted(Counter(
                    s.file for s in w1_bare).most_common())),
                "classification": dict(sorted(Counter(
                    s.classification for s in w1).items())),
                "bare_classification": dict(sorted(w1_bare_cls.items())),
                "bare_tables": len(w1_bare_by_cls),
                "bare_by_table": {tb: sum(c.values())
                                  for tb, c in sorted(w1_bare_by_cls.items())},
                "bare_classification_by_table": {
                    tb: {k: c.get(k, 0) for k in CLS_ORDER}
                    for tb, c in sorted(w1_bare_by_cls.items())},
            },
            "shared_scope": {
                "total": len(shared),
                "bare": sum(1 for s in shared if s.status == _STATUS_BARE),
                "by_host": dict(sorted(Counter(s.host for s in shared).items())),
            },
            "tables": len({t for s in self.sites for t in s.tables}),
            "bare_tables": len({t for s in self.bare for t in s.tables}),
            "table_sites": {t: len(v) for t, v in sorted(self.by_table().items())},
            "classification": dict(sorted(cls_counter.items())),
        }


# ===== 扫描上下文（QA 复审判断项：四件套收拢，判定面不变） =====


@dataclass
class ScopeContext:
    """一个文件作用域的判定上下文四件套（data clump 收拢，纯搬运）。

    - parents / stmt_blocks：全树一次构建，消费形态与 guard 判定共用；
    - handles / tail_guards：按函数作用域派生（guard 判定的句柄面）。
    """

    parents: dict[int, ast.AST]
    stmt_blocks: dict[int, tuple[list, int]]
    handles: dict[str, str]
    tail_guards: list[tuple[int, str]]


# ===== 表归属 =====

def load_ownership() -> dict[str, str]:
    if not OWNERSHIP_MANIFEST.is_file():
        raise FileNotFoundError(f"表归属清单缺失: {OWNERSHIP_MANIFEST}")
    data = json.loads(OWNERSHIP_MANIFEST.read_text(encoding="utf-8"))
    return {k: v for k, v in (data.get("tables") or {}).items()}


# ===== 宿主域 =====

#: 跨进程共享代码前缀 → kernel 桶（src/kernel/ 与 src/domains/contracts/，
#: 后者经 GSB provider 注册可被任意进程加载，进程归属不定）
_KERNEL_PREFIXES = ("src/kernel/", "src/domains/contracts/")


def host_of(rel: str) -> tuple[str | None, str | None]:
    """文件 → (桶标签, 进程域)；未知前缀 → (None, None) 不入清单。

    src/domains/<d>/ → 桶=域=<d>（进程归属固定，§2.2 W1 可比口径）。
    src/api/ 与 src/kernel/ 与 src/domains/contracts/ 为跨进程共享代码
    （被 mgmt 与 runtime 进程同时加载），进程归属不定 → 桶标签单列
    （api/kernel），不并入 W1 裸读分布；其读点仍进清单，同一 execute 在
    部分进程属跨域——视图退役（W4）前须逐点核对，见差异报告。
    其余前缀（scripts/、tests/、src 顶层等）显式跳过：进程归属与 src 无关，
    静默并入 kernel 桶会虚增共享清单。
    """
    m = re.match(r"src/domains/(mgmt|runtime|insight)/", rel)
    if m:
        return m.group(1), m.group(1)
    if rel.startswith("src/api/"):
        return "api", None
    if rel.startswith(_KERNEL_PREFIXES):
        return "kernel", None
    return None, None


# ===== SQL 提取 =====

def sql_text(node: ast.AST) -> tuple[str | None, bool]:
    """提取 execute 第一实参的静态文本。

    返回 (text, dynamic)：完全静态 → (拼接文本, False)；f-string / 非常量拼接 →
    (静态片段拼接文本, True)；无法静态提取 → (None, True)。
    动态部分以 ` ? ` 占位（保持 SQL 段结构，供分类定位）。
    """
    if isinstance(node, ast.Constant):
        return (node.value, False) if isinstance(node.value, str) else (None, True)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                parts.append(" ? ")
            elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
        return "".join(parts), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # 拼接里混入变量（如 `"IN (" + ph + ")"`）→ 变量按 ` ? ` 占位，
        # 保留静态片段（FROM/JOIN 表名仍可提取），dynamic=True
        left, ldyn = sql_text(node.left)
        right, rdyn = sql_text(node.right)
        ltext = left if left is not None else " ? "
        rtext = right if right is not None else " ? "
        return ltext + rtext, bool(ldyn or rdyn or left is None or right is None)
    return None, True


def tables_in(text: str, ownership: dict[str, str]) -> list[str]:
    """SQL 文本里的注册表名（FROM/JOIN 语法位，保序去重）。"""
    out: list[str] = []
    for m in _TABLE_POS_RE.finditer(text):
        name = m.group(4)
        if name.upper() in {"SELECT", "NOT", "EXISTS", "IF", "DUAL"}:
            continue
        if name in ownership and name not in out:
            out.append(name)
    return out


# ===== 作用域与句柄（口径同 scripts/check_lib_query_names.py） =====

def scope_nodes(body: Sequence[ast.stmt]) -> Iterable[ast.AST]:
    """遍历一个作用域，不进入嵌套函数/类/lambda（各自算独立作用域）。"""
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


def _import_aliases(nodes: Iterable[ast.AST], inherited: dict[str, str] | None = None) -> dict[str, str]:
    """作用域内 lib 入口别名（`from src.kernel.domain_read import runtime_lib as x`）
    + 直连 public 模块别名（`from src.domains.<d> import public as x`）→ 域。"""
    aliases: dict[str, str] = dict(inherited or {})
    for node in nodes:
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module.endswith("kernel.domain_read"):
            for a in node.names:
                domain = LIB_FUNCS.get(a.name)
                if domain:
                    aliases[a.asname or a.name] = domain
            continue
        bare = module[len("src."):] if module.startswith("src.") else module
        parts = bare.split(".")
        if (len(parts) == 2 and parts[0] == "domains"
                and any(a.name == "public" for a in node.names)):
            for a in node.names:
                if a.name == "public":
                    aliases[a.asname or a.name] = parts[1]
    return aliases


def _factory_domains(tree: ast.AST, module_aliases: dict[str, str] | None = None) -> dict[str, str]:
    """同文件 `def f(): ... return <别名>[ if ... else None]` 的工厂 → 域（单跳）。"""
    factories: dict[str, str] = {}
    for fn, nodes, aliases in _func_scope(tree, module_aliases):
        if not aliases:
            continue
        for stmt in nodes:
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            domain = _alias_only_domain(stmt.value, aliases)
            if domain:
                factories[fn.name] = domain
                break
    return factories


def _alias_only_domain(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id)
    if isinstance(expr, ast.IfExp):
        return _alias_only_domain(expr.body, aliases) or _alias_only_domain(expr.orelse, aliases)
    return None


def _scope_handles(nodes: Sequence[ast.AST], aliases: dict[str, str],
                   factories: dict[str, str] | None = None) -> dict[str, str]:
    """`x = <别名>()` / `x = <同文件工厂>()` / `x = y`（一跳传播）/ `x = y if c else None` → 域。"""
    handles: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        domain = None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            domain = aliases.get(value.func.id) or (factories or {}).get(value.func.id)
        elif isinstance(value, ast.Name):
            domain = handles.get(value.id) or aliases.get(value.id)
        elif isinstance(value, ast.IfExp):
            for e in (value.body, value.orelse):
                if isinstance(e, ast.Name):
                    domain = aliases.get(e.id) or handles.get(e.id)
                    if domain:
                        break
        if not domain:
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                handles[t.id] = domain
    return handles


def _func_scope(tree: ast.AST, module_aliases: dict[str, str] | None = None):
    """产出 (函数节点, 最内层作用域节点表, 该作用域可用别名)。

    每个函数只看自己的直接语句（scope_nodes 不下钻嵌套函数），
    同一 execute 不会被外层函数重复计点。
    """
    module_nodes = list(scope_nodes(getattr(tree, "body", [])))
    module_aliases = _import_aliases(module_nodes, module_aliases or {})
    # 模块级作用域（装饰器外顶层语句）
    yield None, module_nodes, module_aliases
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes = list(scope_nodes(node.body))
            aliases = _import_aliases(nodes, module_aliases)
            yield node, nodes, aliases


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for nd in ast.walk(tree):
        for ch in ast.iter_child_nodes(nd):
            parents[id(ch)] = nd
    return parents


def _stmt_blocks(tree: ast.AST) -> dict[int, tuple[list, int]]:
    """每条语句 → (所在语句块 list, 下标)——供「紧随语句」消费形态判定。"""
    blocks: dict[int, tuple[list, int]] = {}

    def _register(block: list) -> None:
        for i, stmt in enumerate(block):
            blocks[id(stmt)] = (block, i)

    for nd in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(nd, attr, None)
            if isinstance(block, list):
                _register(block)
        for handler in getattr(nd, "handlers", []) or []:
            _register(handler.body)
    return blocks


def _guard_test_names(test: ast.AST) -> tuple[set[str], bool]:
    """guard 测试式 → (句柄名集合, 是否「句柄为空」方向)。"""
    names: set[str] = set()
    negate = False
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
        names.add(test.left.id)
        negate = any(isinstance(o, ast.Is) for o in test.ops) and any(
            isinstance(c, ast.Constant) and c.value is None for c in test.comparators)
    elif isinstance(test, ast.UnaryOp) and isinstance(test.operand, ast.Name):
        names.add(test.operand.id)
        negate = True
    elif isinstance(test, ast.Name):
        names.add(test.id)
    elif isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute):
        # <x>.available() —— available() 为探活口，调用者为句柄名
        if test.func.attr == "available" and isinstance(test.func.value, ast.Name):
            names.add(test.func.value.id)
    elif (isinstance(test, ast.UnaryOp) and isinstance(test.operand, ast.Call)
          and isinstance(test.operand.func, ast.Attribute)):
        # not <x>.available()
        if test.operand.func.attr == "available" and isinstance(test.operand.func.value, ast.Name):
            names.add(test.operand.func.value.id)
            negate = True
    return names, negate


def _tail_guard_domains(nodes: Sequence[ast.AST], handles: dict[str, str]) -> list[tuple[int, str]]:
    """早退守卫：`if <句柄>[ is not None]: ... return ...`（lib 分支提前返回）。

    该形态下函数尾部的同域 execute 只在 lib 不可用时执行——属兜底（converted）。
    返回 [(guard 行号, 域)]。
    """
    out: list[tuple[int, str]] = []
    for node in nodes:
        if not isinstance(node, ast.If):
            continue
        names, negate = _guard_test_names(node.test)
        if negate:
            continue
        hit = {handles[n] for n in names if n in handles}
        if not hit:
            continue
        # lib 分支包含 return（提前返回）才构成「尾段兜底」
        has_return = any(isinstance(x, ast.Return) for x in ast.walk(node))
        if has_return:
            for d in hit:
                out.append((node.lineno, d))
    return out


def _guarded_domains(call: ast.AST, ctx: ScopeContext) -> set[str]:
    """execute 是否位于 lib 句柄 guard 的兜底分支 → {域}。

    兜底分支 = `if 句柄 is not None:`（或 `if 句柄:`）的 else /
    `if 句柄 is None:` / `if not 句柄:` / `if not 句柄.available():` 的 body /
    早退守卫（lib 分支 return）之后同函数的尾段 execute。
    """
    out: set[str] = set()
    cur: ast.AST = call
    while True:
        par = ctx.parents.get(id(cur))
        if par is None:
            break
        if isinstance(par, ast.If):
            names, negate = _guard_test_names(par.test)
            hit = {ctx.handles[n] for n in names if n in ctx.handles}
            if hit:
                in_else = id(cur) in {id(x) for x in par.orelse}
                if (in_else and not negate) or (not in_else and negate):
                    out |= hit
        cur = par
    for lineno, domain in ctx.tail_guards:
        if call.lineno > lineno:
            out.add(domain)
    return out


# ===== 消费形态分类 =====

def _same_cursor(node: ast.AST, cursor: ast.AST) -> bool:
    return isinstance(node, ast.Name) and isinstance(cursor, ast.Name) and node.id == cursor.id


def _plain_fetch_assign(call: ast.Call, ctx: ScopeContext) -> bool:
    """紧随语句是否为 `X = <同游标>.fetchall()/fetchone()`（auto 可机械改写的消费）。"""
    stmt_id = id(call)
    cur: ast.AST = call
    while True:
        par = ctx.parents.get(id(cur))
        if par is None:
            break
        if id(par) in ctx.stmt_blocks:
            stmt_id = id(par)
            break
        cur = par
    block_info = ctx.stmt_blocks.get(stmt_id)
    if block_info is None:
        return False
    block, idx = block_info
    if idx + 1 >= len(block):
        return False
    nxt = block[idx + 1]
    if not isinstance(nxt, ast.Assign):
        return False
    val = nxt.value
    return (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
            and val.func.attr in {"fetchall", "fetchone"}
            and _same_cursor(val.func.value, call.func.value))


def classify(call: ast.Call, text: str, dynamic: bool, ctx: ScopeContext) -> str:
    """auto / manual 分类（动态 > 非 fetch > auto）。"""
    if dynamic:
        head = text[: text.upper().find(" FROM ")] if " FROM " in text.upper() else text
        if " ? " in head:
            return CLS_DYN_COLUMN
        for m in re.finditer(r"(?i)\bIN\s*\(", text):
            # IN 之后到配对括号内出现占位 → 动态 IN
            seg = text[m.end(): m.end() + 200]
            if " ? " in seg.split(")")[0]:
                return CLS_DYN_IN
        return CLS_DYN_OTHER
    if not _plain_fetch_assign(call, ctx):
        return CLS_NON_FETCH
    return CLS_AUTO


# ===== 主扫描 =====

def scan_source(source: str, filename: str, ownership: dict[str, str],
                repo_root: Path | None = None) -> list[ReadSite]:
    """单文件扫描：返回全部跨域读点（converted + bare，最内层函数归属）。"""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    host_bucket, host_domain = host_of(filename)
    if host_bucket is None:
        return []  # 未知前缀（scripts/、tests/ 等）不入清单（host_of 口径）
    parents = _parent_map(tree)
    stmt_blocks = _stmt_blocks(tree)
    factories = _factory_domains(tree)
    sites: list[ReadSite] = []
    for fn, nodes, aliases in _func_scope(tree):
        handles = _scope_handles(nodes, aliases, factories)
        # 直连 public 模块的别名本身即隐式句柄（`if _rt.available():`）
        guard_handles = {**aliases, **handles}
        tail_guards = _tail_guard_domains(nodes, guard_handles)
        ctx = ScopeContext(parents=parents, stmt_blocks=stmt_blocks,
                           handles=guard_handles, tail_guards=tail_guards)
        for node in nodes:
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"execute", "executemany"}:
                continue
            if not node.args:
                continue
            text, dynamic = sql_text(node.args[0])
            if text is None:
                continue
            if not text.strip().lstrip("(").upper().startswith(("SELECT", "WITH")):
                continue
            tabs = tables_in(text, ownership)
            foreign = sorted({t for t in tabs if ownership[t] != host_domain})
            if not foreign:
                continue
            guarded = _guarded_domains(node, ctx)
            status = _STATUS_CONVERTED if all(ownership[t] in guarded for t in foreign) \
                else _STATUS_BARE
            sites.append(ReadSite(
                file=filename, line=node.lineno, func=fn.name if fn else "<module>",
                host=host_bucket, status=status,
                classification=classify(node, text, dynamic, ctx),
                tables=foreign, dynamic=dynamic,
                sql=re.sub(r"\s+", " ", text)[:120],
            ))
    return sites


def iter_source_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file():
            if p.suffix == ".py":
                out.append(p)
            continue
        for child in sorted(p.rglob("*.py")):
            parts = set(child.parts)
            if "__pycache__" in parts or {".venv", "venv", "node_modules"} & parts:
                continue
            out.append(child)
    return out


def scan_repo(paths: list[Path], ownership: dict[str, str] | None = None) -> Inventory:
    """扫描给定路径（默认 src/），产出清单。"""
    ownership = ownership or load_ownership()
    files = iter_source_files(paths)
    inv = Inventory(files_scanned=len(files))
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(path)
        inv.sites.extend(scan_source(source, rel, ownership))
    inv.sites.sort(key=lambda s: (s.file, s.line))
    return inv


# ===== 渲染 =====

def render_markdown(inv: Inventory) -> str:
    st = inv.stats()
    lines = [
        ">>> cross-domain read inventory (B0 in-repo rebuild)",
        f"    files scanned: {inv.files_scanned}",
        f"    total sites: {st['total']} = bare {st['bare']} + converted {st['converted']}",
        f"    bare by host: {st['bare_by_host']}",
        f"    classification: {st['classification']}",
        "",
    ]
    for table, sites in sorted(inv.by_table().items()):
        lines.append(f"## {table} ({len(sites)} 处)")
        for s in sites:
            lines.append(
                f"- [{s.status}/{s.classification}] {s.file}:{s.line} in {s.func}()")
        lines.append("")
    return "\n".join(lines)


def render_json(inv: Inventory) -> dict:
    return {
        "files_scanned": inv.files_scanned,
        "stats": inv.stats(),
        "sites": [s.as_dict() for s in inv.sites],
    }


# ===== 基线比对（check-all 门禁） =====

def site_keys(sites: Iterable) -> dict[tuple, dict]:
    """读点键 → {status, classification}（扫描器与测试共用的唯一键口径）。

    键含 status/classification 之外的定位信息（file/line/tables）；比对时
    键定位读点、值比对状态——同位置 converted↔bare 翻转属于门禁 FAIL 项。
    """
    out: dict[tuple, dict] = {}
    for s in sites:
        if isinstance(s, dict):
            key = (s["file"], s["line"], tuple(s["tables"]))
            val = {"status": s["status"], "classification": s["classification"]}
        else:
            key = (s.file, s.line, tuple(s.tables))
            val = {"status": s.status, "classification": s.classification}
        out[key] = val
    return out


def compare_baseline(inv: Inventory, baseline_path: Path | None = None) -> tuple[bool, list[str]]:
    """清单与基线快照比对；差异逐条返回（防裸读静默回归）。

    比对键 = (file, line, tables)，另对键上 status/classification 显式比对：
    新增/消失读点、同位置状态翻转（如删除 guard 使 converted→bare）、
    分类漂移均 FAIL。
    """
    path = baseline_path or BASELINE
    if not path.is_file():
        return False, [f"基线快照缺失: {path}"]
    base = json.loads(path.read_text(encoding="utf-8"))
    if base is None:
        return False, ["基线快照缺失: scripts/p4/read_inventory_baseline.json"]
    diffs: list[str] = []
    base_sites = site_keys(base.get("sites", []))
    cur_sites = site_keys(inv.sites)
    for key in sorted(set(base_sites) - set(cur_sites)):
        diffs.append(f"消失的读点: {key[0]}:{key[1]} {list(key[2])}")
    for key in sorted(set(cur_sites) - set(base_sites)):
        v = cur_sites[key]
        diffs.append(f"新增的读点: {key[0]}:{key[1]} {list(key[2])} [{v['status']}/{v['classification']}]")
    for key in sorted(set(base_sites) & set(cur_sites)):
        b, c = base_sites[key], cur_sites[key]
        if b["status"] != c["status"]:
            diffs.append(
                f"状态翻转: {key[0]}:{key[1]} {list(key[2])} "
                f"{b['status']}→{c['status']}（兜底 guard 被删或新增，读点性质变化）")
        elif b["classification"] != c["classification"]:
            diffs.append(
                f"分类漂移: {key[0]}:{key[1]} {list(key[2])} "
                f"{b['classification']}→{c['classification']}")
    return not diffs, diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B0: cross-domain read inventory scanner")
    parser.add_argument("paths", nargs="*", default=["src"], help="待扫路径（默认 src/）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    # 门禁 / 基线维护 / 单表查询三者互斥：过滤视图不得喂门禁，
    # 先写基线再比对必然 PASS（QA 复审 Low 项 fail-fast 收口）
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument("--check", action="store_true",
                     help="对照基线快照（scripts/p4/read_inventory_baseline.json）")
    gate.add_argument("--update-baseline", action="store_true",
                     help="把本次扫描结果写为基线快照（B1 重扫后更新，须同步差异报告）")
    gate.add_argument("--table", metavar="TABLE",
                     help="单表查询：只输出引用该表（须在 configs/table_ownership.json 登记）的读点")
    args = parser.parse_args(argv)
    if args.json and args.update_baseline:
        parser.error("--json 与 --update-baseline 互斥（更新基线为静默维护流程，"
                     "清单输出请单独跑 --json）")

    try:
        ownership = load_ownership()
    except FileNotFoundError as exc:
        print(f">>> FAIL: fail-closed，{exc}")
        return 2
    if args.table is not None and args.table not in ownership:
        print(f">>> FAIL: fail-closed，表 {args.table} 不在归属清单（{OWNERSHIP_MANIFEST}）")
        return 2

    targets = [p if p.is_absolute() else (REPO_ROOT / p) for p in map(Path, args.paths)]
    inv = scan_repo(targets, ownership)
    if not inv.files_scanned:
        print(f">>> FAIL: fail-closed，未扫到任何 .py 文件（{', '.join(args.paths)}）")
        return 2
    if args.table is not None:
        # B1 单表查询：stats 由过滤后清单派生（子集口径）
        inv = Inventory(files_scanned=inv.files_scanned,
                        sites=[s for s in inv.sites if args.table in s.tables])

    if args.json:
        payload = render_json(inv)
        if args.check:
            ok, diffs = compare_baseline(inv)
            payload["baseline_ok"] = ok
            payload["baseline_diffs"] = diffs
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if (not args.check or payload.get("baseline_ok")) else 1
    print(render_markdown(inv))

    if args.update_baseline:
        BASELINE.write_text(json.dumps(render_json(inv), ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f">>> baseline updated: {BASELINE}")
        return 0
    if args.check:
        ok, diffs = compare_baseline(inv)
        if not ok:
            print(">>> FAIL: 读点清单与基线不一致（裸读回归或基线过期）")
            for d in diffs[:50]:
                print(f"      - {d}")
            if len(diffs) > 50:
                print(f"      …… 共 {len(diffs)} 条差异，仅显示前 50 条"
                      "（--json --check 可见全量）")
            return 1
        print(">>> PASS: 读点清单与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
