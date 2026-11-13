"""D102-J：全局同族扫尾——观测页 issue 链接 + 平台显示名查表化。

全局六类排查（patrol 误判教训驱动）收口：
- observability.js _obsIssueUrl/_issueUrl 硬编码 gitcode.com——gitee 仓
  观测页 issue 链接跳错站（用户实弹同族第三处）
- grape_auth.py:2529 p_name 二分——gitee 绑定转移确认页显示原文 'gitee'
已合法冻结（有据）：scene-market/templates（_repoWebHost 优先+降级在位）、
prompt_builder（卡派生优先）、repo_manager API_BASE（gitcode 专属模块，
调用方门控正确）、account.js（目录卡 pat_create_url）。
"""
import re
import pytest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OBS = _REPO_ROOT / "src" / "api" / "frontend" / "app" / "src" / "views" / "ObservabilityView.vue"


@pytest.mark.skip(reason="被测对象随 Vue 迁移下线（本 PR 删除 src/api/static/js/pages/* 与 admin.js）：断言改写到 Vue 源为后续专项，不得以跳过冒充已覆盖")
def test_d102j_observability_urls_derived():
    """观测页 issue 链接统一走 _repoWebHost（硬编码 gitcode.com 防回潮）。"""
    src = _OBS.read_text(encoding="utf-8")
    hits = [l.strip()[:90] for l in src.split("\n")
            if "gitcode.com/" in l and "_repoWebHost" not in l
            and "http" in l and "//" in l]
    assert not hits, f"observability.js 仍有硬编码站点拼接: {hits}"


def test_d102j_grape_auth_pname_lookup():
    """绑定转移确认页平台名查表化（gitee 显示 Gitee 而非原文）。"""
    src = (_REPO_ROOT / "src" / "domains" / "mgmt" / "routes" / "grape_auth.py").read_text(encoding="utf-8")
    assert "'gitee': 'Gitee'" in src, "grape_auth p_name 缺 gitee 查表（D102-J 未实施）"


def test_d102j_cache_version_ge_219():
    """JS 改动配套 bump（本轮 observability 改动 → v219）。"""
    html = (_REPO_ROOT / "src" / "api" / "frontend" / "shell" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"app\.js\?v=(\d+)", html)
    assert m and int(m.group(1)) >= 219, "app.js 版本未随 observability 改动 bump"
