"""守卫：D105 之后禁止用数值比较判断 user_id 归属。

生产实锤（2026-11-12，事件 #9619）：`enqueue_task error '>' not supported between
instances of 'str' and 'int'` —— `_collect_server_env` 里 `if user_id and user_id > 0`
对 UUID 字符串比较直接抛 TypeError，任务当场失败。

D105 把 grape_accounts.id 与 user_id 族列统一成 TEXT/VARCHAR(36)，所以：

* Python 侧一律用 `src.kernel.user_ids.has_user_id()`；
* SQL 侧用 `user_id IS NOT NULL AND user_id NOT IN ('', '0')`
  （SQLite 的 text 排在 integer 之后 → `user_id > 0` 恒真；MySQL 强转 0 → 恒假，
  两种后端都不是"未归属"的语义）。

本用例扫描 src/ 全树：AST 抓 Python 比较表达式（自动跳过注释/docstring），
字符串常量里抓 SQL 片段。仅豁免 runtime_id / skill_id / issue_id 这类真数值列。
"""

import ast
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# 判断"是否归属用户"的变量名（含历史别名）
USER_ID_NAMES = {
    "user_id", "task_user_id", "_task_user_id", "_user_id", "owner_user_id",
    "host_user_id", "_host_uid", "uid", "_uid", "user_uid", "_user_uid",
}

# SQL 里出现即违规（列名限定为 user_id 家族与账号表主键）
SQL_BAD = re.compile(r"\b(user_id)\s*>\s*0\b", re.I)
SQL_BAD_ACCOUNT_PK = re.compile(r"\b(ga\.id|grape_accounts\.id|a\.id|id)\s*>\s*0\b", re.I)


def _py_files():
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        yield p


def _compare_violations(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left] + list(node.comparators)
        for i, op in enumerate(node.ops):
            if not isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE)):
                continue
            left, right = operands[i], operands[i + 1]
            name = getattr(left, "id", None) or getattr(left, "attr", None)
            const = right.value if isinstance(right, ast.Constant) else None
            if name in USER_ID_NAMES and const == 0:
                out.append((node.lineno, f"{name} {type(op).__name__} 0"))
            # 反向写法 0 < user_id
            name_r = getattr(right, "id", None) or getattr(right, "attr", None)
            const_l = left.value if isinstance(left, ast.Constant) else None
            if name_r in USER_ID_NAMES and const_l == 0:
                out.append((node.lineno, f"0 {type(op).__name__} {name_r}"))
    return out


def test_no_numeric_user_id_comparison():
    offenders = []
    for path in _py_files():
        for lineno, expr in _compare_violations(path):
            offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {expr}")
    assert not offenders, (
        "user_id 是 UUID 字符串（D105），数值比较会 TypeError。"
        "请改用 src.kernel.user_ids.has_user_id()：" + "; ".join(offenders)
    )


def _docstring_nodes(tree):
    """收集模块/类/函数的 docstring 节点（说明文字里的 user_id>0 不算违规）。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_numeric_user_id_predicate_in_sql():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in skip:
                    continue
                text = node.value
                if SQL_BAD.search(text):
                    offenders.append(f"{path.relative_to(SRC.parent)}: SQL user_id>0")
                elif SQL_BAD_ACCOUNT_PK.search(text) and "grape_accounts" in text:
                    offenders.append(f"{path.relative_to(SRC.parent)}: SQL 账号主键 id>0")
    assert not offenders, (
        "SQL 里不要用 user_id>0（SQLite 恒真 / MySQL 恒假），"
        "请用 \"user_id IS NOT NULL AND user_id NOT IN ('', '0')\"：" + "; ".join(offenders)
    )


def test_has_user_id_semantics():
    from src.kernel.user_ids import has_user_id

    assert has_user_id("0190f3a1-1b2c-7d3e-8f40-1234567890ab") is True
    assert has_user_id("12") is True
    assert has_user_id(12) is True
    for unset in (None, "", "0", 0, "none", "null", "  "):
        assert has_user_id(unset) is False, unset
    # bool 不是 id（True 会被误判成 1）
    assert has_user_id(True) is False