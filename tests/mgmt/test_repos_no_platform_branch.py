"""结构守卫：repos.py 不得按平台名分派（B 批收口门）。

背景：repos.py 曾有 31 处 `provider == "github"` 式分支，新平台会被 GitHub
分支吞掉。B 批把它们全部换成平台卡字段 / webhook_dialect 分派。本用例用 AST
锁死防线：任何 `xxx_provider == "<平台字面量>"` 形式的新分支都会红。

允许的写法：
  · `_dialect == "github_web"` / `"gitee_v5"`  —— 方言（能力）分派，不是平台名
  · `get_platform_card(provider).xxx`            —— 卡字段
  · provider 与变量比较（不涉及平台字面量）

平台名清单动态取自平台注册表，新增平台自动纳入守卫范围。
"""

import ast
import pathlib

import pytest

REPOS_PY = pathlib.Path(__file__).resolve().parents[2] / "src" / "domains" / "mgmt" / "routes" / "repos.py"


def _platform_names() -> set:
    """在册平台名（卡注册表 ∪ 常见别名），用于识别“平台字面量比较”。"""
    names = {"github", "gitcode", "gitee", "gitlab", "gitea", "github_web", "gitee_v5"}
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms, get_platform_card

        for key in (list_vcs_platforms() or []):
            names.add(str(key))
            card = get_platform_card(str(key))
            for attr in ("name", "webhook_dialect", "host"):
                val = getattr(card, attr, "") or ""
                if isinstance(val, str) and val:
                    names.add(val)
    except Exception:
        pass
    return names


def _provider_like(node) -> bool:
    """左值是否形如 *provider*（provider / _provider / repo_provider / api_provider）。"""
    if isinstance(node, ast.Name):
        return "provider" in node.id.lower()
    if isinstance(node, ast.Attribute):
        if "provider" in node.attr.lower():
            return True
        base = node.value
        return isinstance(base, ast.Name) and "provider" in base.id.lower()
    return False


def _str_const(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _offending_comparisons(tree, platforms: set) -> list:
    """找出 provider 与平台字面量的 ==/!=/in 比较。"""
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            for left, right in zip(operands, operands[1:]):
                lit = _str_const(right) if _provider_like(left) else (_str_const(left) if _provider_like(right) else None)
                if lit in platforms:
                    bad.append((node.lineno, lit))
        elif isinstance(node, (ast.If, ast.IfExp)):
            test = node.test
            # 形如 provider in ("github", "gitcode")
            if isinstance(test, ast.Compare) and any(_provider_like(o) for o in [test.left, *(test.comparators or [])]):
                for comp in test.comparators:
                    if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                        for elt in comp.elts:
                            lit = _str_const(elt)
                            if lit in platforms:
                                bad.append((test.lineno, lit))
    return bad


def test_repos_has_no_platform_name_branch():
    assert REPOS_PY.is_file(), f"repos.py 不存在: {REPOS_PY}"
    tree = ast.parse(REPOS_PY.read_text())
    bad = _offending_comparisons(tree, _platform_names())
    assert not bad, (
        "repos.py 仍存在按平台名分派的分支（应改为平台卡字段 / webhook_dialect 分派）：\n"
        + "\n".join(f"  第 {ln} 行: provider == {lit!r}" for ln, lit in bad)
    )


def test_repos_keeps_dialect_dispatch():
    """反向守卫：方言分派必须仍在（防止有人把分支又写回平台名却不触发上一条）。"""
    tree = ast.parse(REPOS_PY.read_text())
    dialect_cmps = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for left, right in zip([node.left] + list(node.comparators), list(node.comparators)):
                if _str_const(right) in ("github_web", "gitee_v5"):
                    dialect_cmps.append((node.lineno, _str_const(right)))
    assert dialect_cmps, "repos.py 丢失 webhook_dialect 分派（托管主流程应至少按方言分一次）"


@pytest.mark.parametrize("path", [
    "src/domains/mgmt/user/repo_manager.py",
])
def test_repo_manager_platform_literals_are_registry_only(path):
    """repo_manager 允许出现平台名，但只能作为登记表的 key（字典字面量的 Str），
    不得用于 if/elif 分支——否则又回到平台名判定。"""
    root = pathlib.Path(__file__).resolve().parents[2]
    tree = ast.parse((root / path).read_text())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            test = node.test
            if isinstance(test, ast.Compare):
                for left, right in zip([test.left] + list(test.comparators), list(test.comparators)):
                    if _provider_like(left) and _str_const(right) in _platform_names():
                        bad.append((test.lineno, _str_const(right)))
    assert not bad, (
        f"{path} 用平台名做了 if 分支（应改登记表/方言表）：\n"
        + "\n".join(f"  第 {ln} 行: {lit!r}" for ln, lit in bad)
    )


# 这些函数的第 2 位置实参 / provider= 关键字实参就是“平台选择点”。
# 实弹教训（2026-11-13）：remove_managed_repo 里的 to_repo_key(..., provider="github")
# 双探针绕过了比较守卫，仍在按平台名分派；repair_host 裸 to_repo_key 则把 github 仓
# 送去了 gitcode API（404 Project not found）。两类都要锁死。
_PROVIDER_ARG_FUNCS = frozenset({
    "with_oauth_token", "to_repo_key", "create_provider",
    "get_hosting_provider_config", "resolve_bot_username",
    "get_global_bot_token_for", "get_user_token_for",
})


def test_no_platform_literal_as_provider_argument():
    tree = ast.parse(REPOS_PY.read_text())
    platforms = _platform_names()
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name not in _PROVIDER_ARG_FUNCS:
            continue
        for arg in node.args[1:2]:
            if _str_const(arg) in platforms:
                bad.append((node.lineno, f"{name}(...)", _str_const(arg)))
        for kw in (node.keywords or []):
            if kw.arg == "provider" and _str_const(kw.value) in platforms:
                bad.append((node.lineno, f"{name}(provider=)", _str_const(kw.value)))
    assert not bad, (
        "repos.py 在平台选择点写死了平台名（应传 provider 变量 / 走方言派生）：\n"
        + "\n".join(f"  第 {ln} 行: {fn} {lit!r}" for ln, fn, lit in bad)
    )