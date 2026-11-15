#!/usr/bin/env python3
"""把 `src/api/db_setup.py` 里的**迁移 SQL** 按表的归属域搬到 `src/domains/<域>/migrations_ddl.py`。

为什么：`db_setup.py` 是 schema 引导器（api 层），却同时放着 mgmt/runtime/insight 三域的
迁移 SQL。静态扫描（scripts/foreign_table_refs.py）按文件路径判消费域，于是这一个文件
一人贡献 346 处「跨域引用」，把三库独立化的真实依赖水位淹掉了。SQL 属表的归属域
（方案 D9「每域自己的 schema」），下沉到 owner 域模块后：扫描归属 = owner 域本身，
跨域引用归零；db_setup 只留编排与真正跨域的那几条语句。

本脚本与 `scripts/split_schema_ddl.py` 同族：那支搬 `CREATE TABLE` 常量（模块级 DDL），
这支搬**迁移 SQL**（ALTER/UPDATE/INSERT/SELECT 等，含执行列表里的条目）。

做法（AST + 精确文本替换，非正则猜测）：
  1. 遍历 db_setup 全部字符串字面量（跳过 docstring），用与 foreign_table_refs.py 同口径的
     正则取表名，映射到归属域；
  2. **只搬「命中的表全属同一域」的字面量**。一条语句同时牵涉多域 → 留在 db_setup
     （这是真正需要接口化的部分，脚本会打印清单）；
  3. `_migrate_tables()` 里的 `migrations = [...]` 执行列表：把**连续的同一域条目**合成
     一个 tuple 常量（`*runtime_mig.ALTERS_SESSION_EVENT_COLUMNS` 原位展开），行间注释
     一并搬走；列表里跨域/未知归属的条目原样留在原位，**执行顺序完全不变**；
  4. 目标模块按「来源函数」分节，常量名由 SQL 动词 + 主表推导（`UPDATE_AGENT_RUNTIMES`）；
     重名时加 `__<来源函数>` 后缀，避免 `UPDATE_X_2` 之类无语义编号；
  5. 行为零变化：db_setup 仍执行同一批字符串、同一顺序，只是字符串从 owner 域模块 import。

用法：
  python3 scripts/split_migration_sql.py --dry-run   # 只打印计划（默认）
  python3 scripts/split_migration_sql.py --apply     # 落地改写
校验（搬完必做）：
  1. `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --check`
  2. 空库 init_database() 重建后逐表比对列名与搬前快照一致
  3. `pytest tests/arch tests/contracts tests/scripts -q`
"""
from __future__ import annotations

import argparse
import ast
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_SETUP = REPO / "src/api/db_setup.py"
OWNER_TSV = REPO / "docs/weekly-11/table-owner.tsv"

SCHEMA_DOMAIN = {"grape_mgmt_db": "mgmt", "grape_runtime_db": "runtime",
                 "grape_insight_db": "insight"}
DOMAINS = ("mgmt", "runtime", "insight")
# db_setup 里的模块别名（短、可读，且让调用点一眼看出归属域）
ALIAS = {"mgmt": "mgmt_mig", "runtime": "runtime_mig", "insight": "insight_mig"}
IMPORT_ANCHOR = "from src.kernel.session_manager import db, db_lock"

# 与 scripts/foreign_table_refs.py 完全同口径：FROM/JOIN/INTO/UPDATE/DELETE FROM/TABLE <t>
REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|TABLE)\s+"
    r"(?:`?[A-Za-z_][\w$]*`?\s*\.\s*)?`?([A-Za-z_][\w$]*)`?",
    re.I,
)
# 主语动词 → 常量名前缀（长动词在前，避免 CREATE 抢先命中 CREATE INDEX）
VERBS = (
    ("CREATE UNIQUE INDEX", "CREATE_INDEX"),
    ("CREATE INDEX", "CREATE_INDEX"),
    ("CREATE TABLE", "CREATE"),
    ("ALTER TABLE", "ALTER"),
    ("DROP TABLE", "DROP"),
    ("INSERT OR REPLACE INTO", "UPSERT"),
    ("INSERT OR IGNORE INTO", "INSERT_IGNORE"),
    ("INSERT INTO", "INSERT"),
    ("DELETE FROM", "DELETE"),
    ("UPDATE", "UPDATE"),
    ("SELECT", "SELECT"),
    ("PRAGMA", "PRAGMA"),
)
# 主表取词用的正则（与 VERBS 对齐）
SUBJECT_RE = re.compile(
    r"^(?:ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)|DROP\s+TABLE|"
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO|DELETE\s+FROM|UPDATE|SELECT|PRAGMA\s+\w+\()\s*"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[`\"]?(\w+)",
    re.I,
)

# `_migrate_tables()` 执行列表里连续同域段落的名字（按出现顺序，人工命名以便阅读）。
# 段落范围由脚本按 AST 实况推导，这里只给名字——对不上就报错，绝不静默错配。
ALTER_RUN_NAMES = [
    ("runtime", "ALTERS_SESSION_EVENT_COLUMNS"),      # devops/chat session、events、agent_type
    ("runtime", "ALTERS_ENGINE_CREDENTIAL_COLUMNS"),  # runtime_credentials / agent_runtimes 指标
    ("mgmt", "ALTERS_USER_AND_KNOWLEDGE_COLUMNS"),    # user_accounts / agent_shared_knowledge
    ("runtime", "ALTERS_SKILL_ENGINE_COLUMNS"),       # agent_tasks / agent_runtimes / skill
    ("mgmt", "ALTERS_SCENE_COLUMNS"),                 # pipeline_scene / scene_skill_ref / scene_market
    ("runtime", "ALTERS_SCENE_CATEGORY_EVENT_SKIPPED"),
    ("mgmt", "ALTERS_USER_FEEDBACK_COLUMNS"),
]

MODULE_DOC = '''"""迁移 SQL：{domain} 域归属表的 DDL/DML（由 db_setup 拆分而来）。

为什么在域模块里：`src/api/db_setup.py` 是 schema 引导器，却曾放着三域全部迁移 SQL；
静态扫描（scripts/foreign_table_refs.py）按文件路径判消费域，把这些 SQL 全算成
「api 消费外域表」。SQL 属表的归属域（方案 D9「每域自己的 schema」），放 owner 域
模块后扫描结果才反映真实依赖。

搬运工具：`scripts/split_migration_sql.py`（AST 定位字面量，原文搬迁）。
行为不变：`db_setup` 仍按原顺序执行同一批 SQL，只是字符串从本模块 import。
本模块只放**字符串常量**，不含执行逻辑——顺序由 db_setup 的编排保证。
"""
'''


def load_owner() -> dict[str, str]:
    owner: dict[str, str] = {}
    for line in OWNER_TSV.read_text(encoding="utf-8").splitlines():
        schema, _, table = line.partition("\t")
        if table and schema in SCHEMA_DOMAIN:
            owner[table.strip()] = SCHEMA_DOMAIN[schema]
    return owner


def domains_of(text: str, owner: dict[str, str]) -> tuple[set[str], list[str]]:
    """返回 (命中表的归属域集合, 命中表名列表)。"""
    tables = [t for t in REF_RE.findall(text) if t in owner]
    return {owner[t] for t in tables}, tables


def const_name(text: str, tables: list[str]) -> str:
    """由 SQL 动词 + 首个被引用表推导常量名：UPDATE_AGENT_RUNTIMES / SELECT_META ..."""
    head = text.lstrip()
    prefix = "SQL"
    for verb, candidate in VERBS:
        if head.upper().startswith(verb):
            prefix = candidate
            break
    # 主表 = 语句里第一个出现的表（FROM/JOIN/INTO/UPDATE/TABLE <t>），比 "SELECT id" 可靠；
    # 没有命中表时退回动词后的第一个标识符。
    if tables:
        table = tables[0]
    else:
        m = SUBJECT_RE.match(head)
        table = m.group(1) if m else "ROW"
    return re.sub(r"\W+", "_", f"{prefix}_{table.upper()}")


class Rewriter:
    """按 AST 位置做精确文本替换（col_offset 是 UTF-8 字节，需换算成字符）。"""

    def __init__(self, src: str):
        self.src = src
        self.lines = src.splitlines(keepends=True)
        self.line_start: list[int] = []
        pos = 0
        for line in self.lines:
            self.line_start.append(pos)
            pos += len(line)
        self.edits: list[tuple[int, int, str]] = []

    def offset(self, lineno: int, col_bytes: int) -> int:
        line = self.lines[lineno - 1].encode("utf-8")
        return self.line_start[lineno - 1] + len(line[:col_bytes].decode("utf-8"))

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (self.offset(node.lineno, node.col_offset),
                self.offset(node.end_lineno, node.end_col_offset))

    def add(self, start: int, end: int, text: str) -> None:
        self.edits.append((start, end, text))

    def result(self) -> str:
        out = self.src
        for start, end, text in sorted(self.edits, reverse=True):
            out = out[:start] + text + out[end:]
        return out

    def segment(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.src[start:end]


def _dedup(items) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def _lead_comment_lines(src: str, first_lineno: int, stop_lineno: int) -> tuple[int, int, str]:
    """取紧贴某元素上方的连续注释行（遇到空行/代码即停，绝不跨过前一个元素）。

    这些注释原本在描述该段第一条 SQL；段落被折叠成 `*tuple` 后它们会变成"描述整个 tuple"
    的误导性注释，所以随段落一起搬进域模块。
    """
    lines = src.splitlines(keepends=True)
    start = first_lineno - 1          # 0-based，指向元素所在行
    while start - 1 >= max(stop_lineno, 0) and lines[start - 1].strip().startswith("#"):
        start -= 1
    if start == first_lineno - 1:
        return (0, 0, "")
    return (start, first_lineno - 1, "".join(lines[start:first_lineno - 1]))


def _strip_indent(lines: list[str], src_indent: int, dst_indent: int) -> str:
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        body = line[src_indent:] if line[:src_indent].strip() == "" else line.lstrip()
        out.append(" " * dst_indent + body)
    return "\n".join(out)


def collect(owner: dict[str, str]) -> dict:
    src = DB_SETUP.read_text(encoding="utf-8")
    tree = ast.parse(src)
    rw = Rewriter(src)
    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    tops: list[tuple[int, int, str]] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tops.append((n.lineno, n.end_lineno, n.name))

    # PEP 701（3.12）起 f-string 内部的字面量片段也是独立 Constant 节点，
    # 其源码片段**不含引号**（是 f-string 原文的一部分），绝不可当独立常量搬走——
    # 否则会搬出 `NAME = DELETE FROM ...` 这种残句。整条 f-string 属于「运行期拼表名」，
    # 一并留在 db_setup 里走接口化。
    fstring_parts: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for sub in ast.walk(n):
                fstring_parts.add(id(sub))

    def func_of(lineno: int) -> str:
        for a, b, name in tops:
            if a <= lineno <= b:
                return name
        return "<module>"

    # 1) `_migrate_tables()` 里 migrations = [...] 的连续同域段落
    run_spans: list[tuple[ast.Constant, ast.Constant, str, list]] = []
    skip_nodes: set[int] = set()
    lead_of: dict[int, tuple[int, int, str]] = {}   # id(first elt) -> (起行, 止行, 注释原文)
    tables_of: dict[int, list[str]] = {}
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef) or n.name != "_migrate_tables":
            continue
        for node in ast.walk(n):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
                continue
            if not any(getattr(t, "id", "") == "migrations" for t in node.targets):
                continue
            runs: list[list[tuple[ast.Constant, str]]] = []
            prev_end = node.value.elts[0].lineno - 1 if node.value.elts else 0
            for elt in node.value.elts:
                domain = None
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                        and id(elt) not in docstrings and id(elt) not in fstring_parts:
                    doms, _ = domains_of(elt.value, owner)
                    if len(doms) == 1:
                        domain = next(iter(doms))
                if domain is None:
                    runs.append([])
                elif runs and runs[-1] and runs[-1][0][1] == domain:
                    runs[-1].append((elt, domain))
                else:
                    runs.append([(elt, domain)])
                    lead_of[id(elt)] = _lead_comment_lines(src, elt.lineno, prev_end)
                prev_end = elt.end_lineno
            for idx, run in enumerate([r for r in runs if r]):
                domain = run[0][1]
                if idx >= len(ALTER_RUN_NAMES):
                    raise SystemExit(f"ALTER_RUN_NAMES 不够长：第 {idx + 1} 段（{domain}，"
                                     f"{run[0][0].lineno} 行）没有名字")
                want_domain, name = ALTER_RUN_NAMES[idx]
                if want_domain != domain:
                    raise SystemExit(f"ALTER_RUN_NAMES 第 {idx + 1} 段域不匹配："
                                     f"表里 {want_domain}，实际 {domain}（{run[0][0].lineno} 行）")
                elts = [e for e, _ in run]
                run_spans.append((run[0][0], run[-1][0], domain, elts))
                skip_nodes.update(id(e) for e in elts)
                tables_of[id(run[0][0])] = _dedup(
                    t for e in elts for t in domains_of(e.value, owner)[1])

    # 2) 其余单域字面量
    singles: list[tuple[ast.Constant, str]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Constant) or not isinstance(n.value, str):
            continue
        if id(n) in docstrings or id(n) in skip_nodes or id(n) in fstring_parts:
            continue
        doms, _ = domains_of(n.value, owner)
        if len(doms) == 1:
            singles.append((n, next(iter(doms))))

    # 3) 未搬的（多域 / 含外域表）SQL 清单，供回报
    leftover: list[tuple[int, str, list[str], list[str]]] = []
    seen: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and id(n) not in docstrings and id(n) not in fstring_parts:
            doms, tables = domains_of(n.value, owner)
            if len(doms) > 1:
                leftover.append((n.lineno, func_of(n.lineno), sorted(doms), tables))
                seen.add(id(n))
        if isinstance(n, ast.JoinedStr) and id(n) not in seen:
            seg = ast.get_source_segment(src, n) or ""
            doms, tables = domains_of(seg, owner)
            if tables:
                leftover.append((n.lineno, func_of(n.lineno), sorted(doms), tables))

    return {"src": src, "rw": rw, "func_of": func_of, "run_spans": run_spans,
            "singles": singles, "leftover": leftover, "lead_of": lead_of,
            "tables_of": tables_of}


def plan(owner: dict[str, str]) -> dict:
    info = collect(owner)
    used: dict[str, set[str]] = defaultdict(set)

    def alloc(domain: str, base: str, section: str) -> str:
        if base in used[domain]:
            # 重名 → 用来源函数做语义后缀（比 UPDATE_X_2 好读），过长则截断
            slug = re.sub(r"[^A-Za-z0-9]+", "_", section).upper().strip("_")[:28].rstrip("_")
            base = f"{base}__{slug}"
        name, n = base, 2
        while name in used[domain]:
            name = f"{base}_{n}"
            n += 1
        used[domain].add(name)
        return name

    # 段落名直接用配置表里的名字（名字本身唯一，与段落出现顺序一一对应）
    info["run_names"] = {}
    names_by_domain = defaultdict(list)
    for domain, nm in ALTER_RUN_NAMES:
        names_by_domain[domain].append(nm)
    cursor: dict[str, int] = defaultdict(int)
    for first, last, domain, _ in info["run_spans"]:
        idx = cursor[domain]
        cursor[domain] += 1
        name = names_by_domain[domain][idx]
        if name in used[domain]:
            raise SystemExit(f"段落常量名重复：{name}")
        used[domain].add(name)
        info["run_names"][id(first)] = (domain, name)

    info["single_names"] = {}
    for node, domain in info["singles"]:
        base = const_name(node.value, domains_of(node.value, owner)[1])
        info["single_names"][id(node)] = (
            domain, alloc(domain, base, info["func_of"](node.lineno)))
    return info


def render_module(domain: str, info: dict) -> str:
    src, rw = info["src"], info["rw"]
    items: list[tuple[int, str, str]] = []   # (行号, 节名, 常量源码)

    for first, last, dom, _ in info["run_spans"]:
        if dom != domain:
            continue
        name = info["run_names"][id(first)][1]
        start, _ = rw.span(first)
        _, end = rw.span(last)
        lead_start, lead_end, _ = info["lead_of"].get(id(first), (0, 0, ""))
        lead = "" if not lead_start else \
            _strip_indent(src.splitlines()[lead_start:lead_end], 8, 4) + "\n"
        body = lead + _strip_indent(src[start:end].splitlines(), 8, 4)
        items.append((first.lineno, info["func_of"](first.lineno), f"{name} = (\n{body},\n)\n"))

    for node, dom in info["singles"]:
        if dom != domain:
            continue
        name = info["single_names"][id(node)][1]
        seg = rw.segment(node)
        if seg.startswith(('"""', "'''")):
            body = f"{name} = {seg}\n"
        elif "\n" in seg:
            body = f"{name} = (\n{_strip_indent(seg.splitlines(), node.col_offset, 4)}\n)\n"
        else:
            body = f"{name} = {seg}\n"
        items.append((node.lineno, info["func_of"](node.lineno), body))

    chunks = [MODULE_DOC.format(domain=domain)]
    prev = None
    for _, section, body in sorted(items, key=lambda x: x[0]):
        if section != prev:
            chunks.append(f"\n# ── 自 db_setup.{section}() 下沉 "
                          f"{'─' * max(0, 46 - len(section))}\n")
            prev = section
        chunks.append(body)
    return "".join(chunks)


def apply(owner: dict[str, str], info: dict) -> None:
    rw = info["rw"]
    lines = info["src"].splitlines(keepends=True)
    line_start = info["rw"].line_start
    for first, last, domain, _ in info["run_spans"]:
        name = info["run_names"][id(first)][1]
        start, _ = rw.span(first)
        _, end = rw.span(last)
        tables = info["tables_of"].get(id(first), [])
        hint = f"  # 表：{', '.join(tables)}" if tables else ""
        rw.add(start, end, f"*{ALIAS[domain]}.{name},{hint}")
        lead_start, lead_end, _ = info["lead_of"].get(id(first), (0, 0, ""))
        if lead_start:
            # 注释随段落进域模块（否则会变成"描述整段"的误导注释）
            rw.add(line_start[lead_start], line_start[lead_end], "")

    for node, domain in info["singles"]:
        name = info["single_names"][id(node)][1]
        start, end = rw.span(node)
        rw.add(start, end, f"{ALIAS[domain]}.{name}")

    out = rw.result()
    imports = "".join(f"from src.domains.{d} import migrations_ddl as {ALIAS[d]}\n"
                      for d in DOMAINS)
    assert IMPORT_ANCHOR in out, "import 锚点缺失：db_setup 头部结构已变，脚本需更新"
    out = out.replace(IMPORT_ANCHOR, imports + IMPORT_ANCHOR, 1)

    DB_SETUP.write_text(out, encoding="utf-8")
    for domain in DOMAINS:
        target = REPO / f"src/domains/{domain}/migrations_ddl.py"
        target.write_text(render_module(domain, info), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="迁移 SQL 按域下沉")
    ap.add_argument("--apply", action="store_true", help="落地改写（默认只打印计划）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划（默认行为，显式更清楚）")
    args = ap.parse_args(argv)

    owner = load_owner()
    info = plan(owner)
    single_refs = sum(len(domains_of(n.value, owner)[1]) for n, _ in info["singles"])
    run_refs = sum(len(domains_of("\n".join(e.value for e in elts), owner)[1])
                   for _, _, _, elts in info["run_spans"])
    run_entries = sum(len(elts) for _, _, _, elts in info["run_spans"])
    print(f"单条字面量 {len(info['singles'])} 条 → {single_refs} 处表引用")
    print(f"执行列表段落 {len(info['run_spans'])} 段 / {run_entries} 条 → {run_refs} 处表引用")
    for first, last, domain, elts in info["run_spans"]:
        print(f"  {domain:8s} {first.lineno:5d}-{last.end_lineno:<5d} {len(elts):3d} 条 → "
              f"{info['run_names'][id(first)][1]}")
    print(f"合计下沉 {single_refs + run_refs} 处，预计 db_setup 残留 "
          f"{sum(len(t) for _, _, _, t in info['leftover'])} 处左右")
    print(f"\n未搬（跨域/含外域表）SQL {len(info['leftover'])} 处：")
    for lineno, func, doms, tables in sorted(info["leftover"]):
        print(f"  {lineno:5d} {func:46s} {','.join(doms):18s} {','.join(tables)[:70]}")

    if not args.apply:
        print("\n（dry-run：未写文件；加 --apply 落地）")
        return 0
    apply(owner, info)
    print("\n已落地：src/api/db_setup.py + src/domains/*/migrations_ddl.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())