"""Vue 外壳 + 引擎详情端点的存活性断言（从废弃的老前端用例里摘出来的有效断言）。

背景（2026-11-11 arch-weekly-11 合入）：
- 老页面源码（src/api/static/js/pages/*.js）已随 Vue 迁移删除，arch 侧也不存在，
  因此以「读老 JS 源码」为前提的用例（#294/#307/#337/#364/#406/#182/#runtimes_stats_i18n）
  整批退役；其中两条与前端源码无关的有效断言在此重建：
  · #337 的「index.html 版本号已提升」→ 改为断言 Vue chunk 引用带版本号（缓存刷新契约）
  · #364 的「settings.py 引擎详情端点」→ 改为断言迁移后的 canonical 模块仍具备该端点
"""
import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
INDEX_HTML = os.path.join(ROOT, "src", "api", "frontend", "legacy", "index.html")
SETTINGS_PY = os.path.join(ROOT, "src", "domains", "mgmt", "routes", "settings.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_vue_chunks_carry_cache_busting_version():
    """index.html 必须引用 Vue 视图 chunk 且带 ?v= 版本号（原 #337 的缓存刷新契约）。"""
    html = _read(INDEX_HTML)
    for chunk in ("issues-view", "dashboard-view", "runtimes-view", "event-view"):
        m = re.search(r"/static/js/vue/%s\.js\?v=(\d+)" % re.escape(chunk), html)
        assert m, f"index.html 缺少 {chunk}.js 的版本化引用"
        assert int(m.group(1)) >= 1


def test_engine_detail_endpoint_still_exposed():
    """#364：settings 域模块保留引擎详情端点（复用列表可见性口径 + 404 分支）。"""
    src = _read(SETTINGS_PY)
    assert "get_agent_runtime_detail" in src, "settings.py 缺少详情端点函数"
    assert 'operation_id="settings_agent_runtime_detail"' in src, "详情端点 operation_id 缺失"
    assert "get_agent_runtimes(user=user, page=1, page_size=0" in src, "未复用列表可见性口径"
    assert "status_code=404" in src, "不可见/未命中应 404"