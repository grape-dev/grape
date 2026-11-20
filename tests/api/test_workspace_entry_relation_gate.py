"""入口判据守卫：工作区 / 我的项目 / 项目模板 必须先看「用户↔平台关系表」。

用户口径（2026-11-16）：进入这三个页面之前要先判断是否存在用户平台仓库关联关系，
没有关系就直接进新手引导页。判据的唯一真源 = `grape_t_user_repo_platform_link`
（未撤销行），前端拿 `/auth/platform-links` 的快照做门。

本锁四件事：
  1. 后端关系读口确实查关系表（不是靠 identities 猜）；
  2. 三处前端入口都用 `_hasVcsBinding()` 做门：启动恢复、登录后恢复、侧栏导航；
  3. 进页前先刷新快照（refreshRepoLinks 不能是死代码），避免「清 PAT 后旧快照仍说已接入」；
  4. 关系行残留（来源全无）时读口自愈撤销（反向自愈）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "api" / "frontend" / "shell" / "js" / "app.js"
AUTH_PY = ROOT / "src" / "domains" / "mgmt" / "routes" / "auth.py"


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_backend_reads_relation_table():
    src = AUTH_PY.read_text(encoding="utf-8")
    i = src.index("def _repo_platform_links")
    seg = src[i:i + 2000]
    assert "UserRepoPlatformLinkRepo.list_by_user" in seg, (
        "关系读口未查 grape_t_user_repo_platform_link（判据必须来自关系表）")
    assert "_prune_dangling_links" in seg, (
        "关系读口缺反向自愈：来源全无的残留关系必须撤销，否则前端拿幽灵关系判已接入")


def test_frontend_gate_uses_relation_snapshot():
    src = _app_js()
    i = src.index("function _hasVcsBinding()")
    seg = src[i:i + 900]
    assert "window.__repoPlatformLinks" in seg, "前端门未使用关系快照"
    assert "'table'" in seg or '"table"' in seg, "前端门未按关系表来源判定"


def test_three_entries_are_gated():
    src = _app_js()
    # ① 启动恢复 ② 登录后恢复 ③ 侧栏 nav 的仓库依赖视图
    assert src.count("_hasVcsBinding()") >= 5, "关系门覆盖不足（应为 启动/恢复/nav/引导页等多处）"
    for marker in ("function _bootRestore", "function _restoreNavAfterLogin", "function nav("):
        i = src.index(marker)
        seg = src[i:i + 2600]
        assert "_hasVcsBinding()" in seg, f"{marker} 里没有关系门"


def test_snapshot_refreshed_before_entering_pages():
    src = _app_js()
    # refreshRepoLinks 必须有调用点（此前是死代码），且入口在进页前刷新
    assert len(re.findall(r"refreshRepoLinks\(", src)) >= 3, (
        "refreshRepoLinks 只定义未调用（快照永远停在登录那一刻）")
    i = src.index("function loadRepos(")
    seg = src[i:i + 1200]
    assert "_ensureRepoLinksFresh" in seg, "loadRepos（我的项目/项目模板入口）进页前未刷新关系"
    assert "goToRepos()" in seg, "loadRepos 无关系时未转引导页"
    j = src.index("function goToRepos(")
    seg2 = src[j:j + 900]
    assert "refreshRepoLinks(" in seg2, "goToRepos（仓库大厅入口）未刷新关系快照"


def test_nav_repo_views_redirect_to_guide():
    src = _app_js()
    i = src.index("var _repoDependentViews")
    seg = src[i:i + 1200]
    assert "goToRepos()" in seg, "仓库依赖视图无关系时未跳引导页"