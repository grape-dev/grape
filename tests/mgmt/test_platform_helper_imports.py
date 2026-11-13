"""平台卡 helper 的「出生即坏」门禁（2026-11-13）。

背景：`src/domains/runtime/routes/issues.py` 里调了 `host_for_repo_key()`，但该文件
从未 import 它 —— 每次请求澄清历史都 NameError → 500；base 5aa5d1bd0 同样，属
「出生即坏」。仓库既有的静态扫描脚本只覆盖固定模块（src.domains.mgmt.user.db），
抓不到这类新增文件。

本用例把平台卡里会被业务侧直接调用的 helper 名做成清单，扫全仓：**任何文件里出现在
调用位置却没有任何 import / 定义绑定**，即失败。宁可误报也不要静默 500。
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# platform_card 里被业务侧直接引用的公共 helper（新增导出时把名字加进来）
PLATFORM_HELPERS = (
    "host_for_repo_key",
    "display_name_for_repo_key",
    "get_platform_card",
    "list_vcs_platforms",
)


def _files():
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in str(p)]


def _bound(src: str, name: str) -> bool:
    """文件里是否有该名的绑定：import / from-import / def / 赋值。"""
    pats = (
        rf"^\s*from\s+\S+\s+import\s+[^\n]*\b{name}\b",   # from x import name / (a, name)
        rf"^\s*import\s+[^\n]*\b{name}\b",
        rf"^\s*def\s+{name}\s*\(",
        rf"^\s*{name}\s*=",
        rf"\b{name}\s*,",                                  # 多重 import 续行
    )
    return any(re.search(p, src, re.M) for p in pats)


def test_platform_helpers_are_bound_where_called():
    missing = []
    for path in _files():
        src = path.read_text(encoding="utf-8", errors="ignore")
        for name in PLATFORM_HELPERS:
            if not re.search(rf"\b{name}\s*\(", src):
                continue          # 没调用就不用管
            if re.search(rf"^\s*def\s+{name}\s*\(", src, re.M):
                continue          # 本文件就是定义处
            if not _bound(src, name):
                missing.append(f"{path.relative_to(SRC.parent)}: 调用 {name}() 但无 import/绑定")
    assert not missing, "平台卡 helper 未绑定即调用（运行时 NameError → 500）：\n" + "\n".join(missing)


def test_issue_url_strips_provider_prefix():
    """澄清历史返回的 issue_url 拼接前必须拆掉平台前缀（repo_key 形态 gitee:owner/repo）。"""
    text = (SRC / "domains/runtime/routes/issues.py").read_text(encoding="utf-8")
    assert "split_owner_repo(repo_key)" in text, "issue_url 仍直接拼 repo_key（会拼出 gitee:owner/repo）"
    assert "/{repo_key}/issues/" not in text, "issue_url 仍在用带前缀 repo_key"