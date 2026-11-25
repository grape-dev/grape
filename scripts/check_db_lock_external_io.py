#!/usr/bin/env python3
"""锁内外部分 IO 静态检查（Issue #4295，棘轮）。

背景（Issue #4295 生产实证 2026-11-24 19:00–20:00）：``src/kernel/session_manager.py``
的 ``db_lock`` 是**进程级单把 RLock**，全仓 ``with db_lock:`` 覆盖几乎所有 DB 路径。
一旦某个临界区里做了外部 IO（VCS/GitCode 翻页、IM 通知投递、webhook 回帖），
单请求就能把整个进程的 DB 访问串行化——现网实测：前台请求
``owner=/api/issues/shawn1235/hello-skill`` 持有 99.90s，同期
``POST /api/repos/host`` 两条 502（47.9s / 56.2s）。

本脚本把「锁内不许做外部 IO」变成提交前就能失败的**门禁**，而不是等下一次 100s
卡顿再抓 py-spy：

  while 遍历全部 ``with db_lock:``（含 ``db_lock`` 别名 ``_lock``/``lock``），
  块体内**不跨嵌套函数**地收集调用名，命中外部 IO 名单即报违规。

判定名单是高精度的（只报确定的远程/进程外调用）：函数名或属性名包含
``notify`` / ``vcs`` / ``gitcode`` / ``github`` / ``gitlab`` / ``feishu`` /
``dingtalk`` / ``wecom`` / ``slack`` / ``telegram`` / ``webhook`` /
``urlopen`` / ``requests`` / ``httpx`` / ``aiohttp`` / ``subprocess`` / ``_client``。

棘轮：存量违规记在 ``configs/db_lock_external_io_baseline.json``。新增即失败；
清理后用 ``--update-baseline`` 收紧（与 ``scripts/check_async_blocking.py`` 同款做法）。

用法：
  python3 scripts/check_db_lock_external_io.py                  # 检查（棘轮）
  python3 scripts/check_db_lock_external_io.py --list           # 打印全部命中
  python3 scripts/check_db_lock_external_io.py --update-baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
BASELINE = REPO / "configs" / "db_lock_external_io_baseline.json"

# 锁实体名（无条件认定为全局 db_lock）
LOCK_NAMES = {"db_lock", "_db_lock"}
# 惯用的远程客户端变量名（`def f(client)` 这类入参没有"工厂赋值"可供绑定识别）
IO_VAR_NAMES = frozenset({"client", "_client", "vcs", "_vcs", "vcs_client"})

# 逐文件额外别名（`db, lock = _db()` 之类静态无法自证的形态，按文件登记）
EXTRA_LOCK_ALIASES = {
    "src/domains/mgmt/pipeline_config.py": {"lock"},  # _db() 返回 (db, db_lock)
}

# 名字带外部 IO 关键词、但实现只碰本进程/本地文件（DDL 建表、DB 映射查询、纯函数）
# 的符号白名单——按**完整点分名**登记，避免"名字像 IO"掩盖真正新引入的远程调用。
LOCAL_ONLY_ALLOWLIST = frozenset({
    "ensure_notify_outbox_schema",          # im/engine_wiring：notify outbox 表 DDL
    "list_vcs_platforms",                   # vcs/platform_card：平台卡缓存（本地 configs）
    "_get_gitcode_user_by_user_id",         # issues_center：um 身份列查询
    "_mgmt_alerts.mapped_feishu_user_ids",  # mgmt public：unified_user_mapping 查询
    "_policy.in_notify_window",             # alert_policy：纯函数
    "_policy.parse_notify_window",          # alert_policy：纯函数
    "_accounts.bind_feishu_mapping_row",    # mgmt public：映射表写入
})

EXTERNAL_IO_TOKENS = (
    "notify", "vcs", "gitcode", "github", "gitlab", "feishu", "dingtalk",
    "wecom", "slack", "telegram", "webhook", "urlopen", "requests", "httpx",
    "aiohttp", "subprocess", "_client",
)


def _file_lock_aliases(tree: ast.AST, rel: str) -> set[str]:
    """本文件里指向全局 db_lock 的绑定名（含 import 别名与 `X = db_lock`）。

    不做"任何叫 lock 的变量都是 db_lock"的宽松判定——`with lock:` 在
    ``src/api/deps.py`` 等处是**用户级 threading.Lock**，宽松判定会把正常代码
    误报成"锁内外部 IO"，门禁随后会被放宽到失去意义。
    """
    aliases = set(LOCK_NAMES) | set(EXTRA_LOCK_ALIASES.get(rel, set()))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in LOCK_NAMES:
                    aliases.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            val = node.value
            is_lock = ((isinstance(val, ast.Name) and val.id in LOCK_NAMES)
                       or (isinstance(val, ast.Attribute) and val.attr in LOCK_NAMES))
            if is_lock:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        aliases.add(tgt.id)
    return aliases


def _call_name(node: ast.Call) -> str:
    """(dotted name)。``a.b.c()`` → "a.b.c"，``f()`` → "f"。"""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    parts: list[str] = []
    cur: ast.AST = f
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):
        parts.append("<call>")
    return ".".join(reversed(parts))


# DB 游标/连接本地方法：即使变量名像远程客户端，这些方法也不出进程
DB_LOCAL_METHODS = frozenset({
    "fetchone", "fetchall", "fetchmany", "fetch", "execute", "executemany",
    "cursor", "close", "commit", "rollback", "description", "rowcount",
})


def _is_external_io(name: str, io_vars: frozenset[str] = frozenset()) -> bool:
    if name in LOCAL_ONLY_ALLOWLIST:
        return False
    parts = name.split(".")
    if parts[-1] in DB_LOCAL_METHODS:
        return False
    if io_vars and parts[0] in io_vars:
        return True  # `client = _pr_vcs_client(...)` 后的 client.<any>() 都是远程调用
    low = name.lower()
    return any(tok in low for tok in EXTERNAL_IO_TOKENS)


def _is_client_factory(name: str) -> bool:
    """``_pr_vcs_client(...)`` / ``get_vcs_client(...)`` 这类"造远程客户端"的调用。

    只认工厂名（以 client/vcs 收尾），不认 ``client.list_issues()`` 这类方法调用——
    否则 ``c = client.list_issues()`` 会把游标类变量也当远程客户端，误报 c.fetchone()。
    """
    last = name.rsplit(".", 1)[-1].lower()
    return last.endswith("client") or last in ("get_vcs", "vcs")


def _file_io_aliases(tree: ast.AST) -> frozenset[str]:
    """本文件里"装着远程客户端"的变量名（`client = _pr_vcs_client(repo_key)`）。

    名字本身不带关键词（``client.list_issues``），只靠 token 匹配会漏；绑定侧识别
    才能把这类远程调用抓进"锁内外部 IO"。
    """
    names: set[str] = set(IO_VAR_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _is_client_factory(_call_name(node.value)):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
    return frozenset(names)


class _Scanner(ast.NodeVisitor):
    """收集每个 ``with db_lock:`` 块体内的外部 IO 调用。"""

    def __init__(self, path: str, lock_aliases: set[str],
                 io_vars: frozenset[str] = IO_VAR_NAMES) -> None:
        self.path = path
        self.lock_aliases = lock_aliases
        self.io_vars = io_vars
        self.scope: list[str] = []
        self.violations: list[dict] = []

    def _is_lock_expr(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.lock_aliases
        if isinstance(node, ast.Attribute):
            return node.attr in self.lock_aliases
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        locked = any(self._is_lock_expr(item.context_expr) for item in node.items)
        if locked:
            body_calls: list[tuple[str, int]] = []
            for stmt in node.body:
                self._collect(stmt, body_calls)
            for name, lineno in body_calls:
                if _is_external_io(name, self.io_vars):
                    self.violations.append({
                        "file": self.path,
                        "function": self.scope[-1] if self.scope else "<module>",
                        "lineno": lineno,
                        "call": name,
                    })
        self.generic_visit(node)

    def _collect(self, node: ast.AST, out: list[tuple[str, int]]) -> None:
        """块体内收集调用名；不跨嵌套 def/async def（它们是独立函数，各自判定）。"""
        stack: list[ast.AST] = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(cur, ast.Call):
                name = _call_name(cur)
                if name:
                    out.append((name, getattr(cur, "lineno", 0)))
            stack.extend(ast.iter_child_nodes(cur))


def scan_file(path: Path) -> list[dict]:
    rel = path.relative_to(REPO).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover - 语法错误另有门禁
        print(f"[skip] {rel}: {e}", file=sys.stderr)
        return []
    scanner = _Scanner(rel, _file_lock_aliases(tree, rel), _file_io_aliases(tree))
    scanner.visit(tree)
    return scanner.violations


def scan_tree(root: Path = SRC) -> list[dict]:
    found: list[dict] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found.extend(scan_file(path))
    return found


def _key(v: dict) -> str:
    return f"{v['file']}:{v['function']}:{v['call']}"


def load_baseline() -> dict[str, int]:
    if not BASELINE.exists():
        return {}
    try:
        raw = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # pragma: no cover
        return {}
    counts = raw.get("counts") or {}
    return {str(k): int(v) for k, v in counts.items()} if isinstance(counts, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="db_lock 内不得做外部 IO（Issue #4295 门禁）")
    ap.add_argument("--list", action="store_true", help="打印全部命中明细")
    ap.add_argument("--update-baseline", action="store_true", help="用当前命中重写基线（收紧用）")
    args = ap.parse_args(argv)

    violations = scan_tree()
    counts: dict[str, int] = {}
    for v in violations:
        counts[_key(v)] = counts.get(_key(v), 0) + 1

    if args.list:
        for v in violations:
            print(f"{v['file']}:{v['lineno']} {v['function']} -> {v['call']}")
        print(f"total={len(violations)}")

    if args.update_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"issue": 4295, "counts": dict(sorted(counts.items()))},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"baseline updated: {BASELINE.relative_to(REPO)} ({len(counts)} entries)")
        return 0

    baseline = load_baseline()
    regressions = [k for k, n in counts.items() if n > baseline.get(k, 0)]
    if regressions:
        print("FAIL: db_lock 临界区内出现外部 IO（Issue #4295 门禁）——"
              "把远程/通知调用移到 with db_lock 之外（锁内取快照 → 锁外 IO → 短写回）:")
        for k in sorted(regressions):
            print(f"  +{counts[k] - baseline.get(k, 0)} {k}")
        for v in violations:
            if _key(v) in regressions:
                print(f"    {v['file']}:{v['lineno']} in {v['function']}: {v['call']}()")
        return 1

    tightened = [k for k, n in baseline.items() if n > counts.get(k, 0)]
    if tightened:
        print("OK（基线可收紧：" + ", ".join(sorted(tightened)) + " → 建议 --update-baseline）")
    elif not violations:
        print("OK: db_lock 临界区内无外部 IO（0 处命中）")
    else:
        print(f"OK: 无锁内外部 IO 新增（当前命中 {len(violations)} 处，均在基线内）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())