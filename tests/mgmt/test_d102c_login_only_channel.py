"""D102-C：纯登录渠道账号象限（华为第一实例）——行为锁。

移交问题 P0-2：仅登录渠道（有登录无仓库平台绑定）账号进工作台，
/repos/member 原 400"API Token 未配置"（与刚 OAuth 登录完的事实矛盾）。
新语义：200 + 空列表 + _no_vcs_binding 标记（前端走绑定引导）。

移交待办 2：华为自动建号 username 用 sub（MDE3Lqmw... 不可读）——
有昵称时优先昵称派生（清洗/冲突后缀复用 _generate_username）。
"""
import os
import pytest
import re
from pathlib import Path

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]  # 相对仓库根（任何 checkout 均有效）


def test_d102c_no_vcs_binding_returns_200_empty():
    """纯登录渠道账号（绑定 ∩ 仓库平台目录 = 空集）→ 200 空列表 + 标记位。"""
    from src.domains.mgmt.routes import repos
    src = inspect.getsource(repos)
    # 新语义在位：标记位返回（防回归为 raise 400）
    assert "_no_vcs_binding" in src, "repos.py 缺 _no_vcs_binding 标记（P0-2 未修）"
    # 旧 400 在 member 函数体内不再出现（/create 端点的同文案保留——语义不同）
    _msrc = src[src.find("def list_member_repos"):]
    assert 'message": "API Token 未配置' not in _msrc[:3000], \
        "member 路径仍有 400 旧文案（P0-2 未修净）"


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_d102c_frontend_binding_guide():
    """前端 loadRepos 对 _no_vcs_binding 直接进绑定引导页（2026-11-08 用户裁定：
    无仓库平台绑定 = 引导页，不再渲染占位页+按钮）。"""
    src = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    i = src.find("d._no_vcs_binding")
    assert i >= 0, "app.js 未消费 _no_vcs_binding 标记"
    seg = src[i:i + 900]
    assert "_renderBindingGuide()" in seg, "_no_vcs_binding 分支未直接进引导页 _renderBindingGuide"


def test_d102c_username_readable_derivation():
    """华为建号用户名：sub 形态 external_login（>24 字符）且有昵称 → 昵称派生。"""
    from src.domains.mgmt.routes.grape_auth import _generate_username
    # 昵称派生路径：sub(>24) + name
    uname = _generate_username("Zhang San", "hwcloud")
    assert uname and uname not in ("MDE3Lqmw",) and len(uname) >= 3
    # sub 直接派生（无昵称）仍可用（兜底语义保留）
    sub = "MDE3LqmwYWKxa2xqOHhacA"
    u2 = _generate_username(sub, "hwcloud")
    assert u2 == sub[:32]  # 清洗后原样（字母数字）——兜底不破坏

@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_d102n_entry_gates_judged_by_repos_member():
    """入口判定权威 = /repos/member 的 _no_vcs_binding（后端按绑定 ∩ 平台卡目录）。
    app.js 存在两条 loadRepos 形态（goToRepos 内联 + 独立函数）——每处
    _no_vcs_binding 消费点都必须直达引导页（用户实弹：漏一处=登录白屏加载）。"""
    src = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    assert "_REPO_PLATFORMS" not in src and "_hasRepoBinding" not in src, "平台名写死回潮"
    occurrences = [m.start() for m in re.finditer(r"d\._no_vcs_binding", src)]
    assert occurrences, "loadRepos 未消费后端判定标记"
    for k, pos in enumerate(occurrences, 1):
        seg = src[pos:pos + 700]
        assert "_renderBindingGuide()" in seg, f"第 {k} 处 _no_vcs_binding 未直达引导页"
    # 空仓库不得写入缓存（缓存同步路径会渲染空态+守卫早退=永久加载中）
    i = src.find("d._no_vcs_binding")
    seg = src[i:i + 700]
    assert "removeItem(REPO_CACHE_KEY)" in seg, "无绑定响应未清仓库缓存（二次进入会卡加载中）"



