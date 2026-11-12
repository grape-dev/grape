"""S103 场景用例（D77）：知识读侧候选集 + 前端二分残留收口——场景级验收。

UC↔用例映射（requirements"候选集占位符家族场景用例"表续编）：
- UC8 知识读侧前缀不一致（P2 修复闭环）：裸键/异前缀入参命中既有 gitcode: 行
  → test_uc8_knowledge_read_candidates_live
- UC9 前端 provider/host 卡派生（源码守卫，JS 无法 pytest 实弹——
  仓库既定口径 = 源码断言 + node --check 部署验证）→ test_uc9_frontend_binary_gone
- UC10 scene-market 平台信息下发链路（后端 payload 补 source_platform）
  → test_uc10_scene_market_platform_shipped
- UC11 index.html 缓存版本纪律（?v= 只升不降）→ test_uc11_cache_version_bumped
"""
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SRC = Path(__file__).resolve().parent.parent / "src"
_WEB = _SRC / "api" / "static"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def test_uc8_knowledge_read_candidates_live():
    """UC8：知识读侧前缀不一致实弹——库中行存 gitcode: 前缀，读侧传裸键/
    异前缀（github:）均能命中（修复前精确匹配 → 静默丢注入）。"""
    from src.api.knowledge import _query_knowledge_obs
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE agent_shared_knowledge (id INTEGER PRIMARY KEY, repo_key TEXT, "
                 "scope TEXT, agent_type TEXT, issue_num INTEGER, status TEXT, archived INTEGER, "
                 "obs_key TEXT, content_hash TEXT, name TEXT)")
    conn.execute("INSERT INTO agent_shared_knowledge (repo_key, scope, status, archived, obs_key, content_hash) "
                 "VALUES ('gitcode:o/r', 'baseline', 'accepted', 0, 'obs/k1', 'h1')")
    with patch("src.api.knowledge.db", conn), \
         patch("src.api.knowledge.db_lock", threading.RLock()):
        # 裸键入参命中前缀行
        assert _query_knowledge_obs("o/r", "baseline", "", 0) == ("obs/k1", "h1")
        # 异前缀入参命中
        assert _query_knowledge_obs("github:o/r", "baseline", "", 0) == ("obs/k1", "h1")
        # 本身前缀命中（既有形态不变）
        assert _query_knowledge_obs("gitcode:o/r", "baseline", "", 0) == ("obs/k1", "h1")
        # 完全无关仓 → 空（不误命中）
        assert _query_knowledge_obs("gitcode:other/x", "baseline", "", 0) == ("", "")


def test_uc9_frontend_binary_gone():
    """UC9：前端二分残留收口源码守卫——四站点改走卡目录载体。"""
    repos = _read("api/static/js/pages/repos.js")
    # hostAndSelect 零 github/gitcode 三元硬编码
    seg = repos[repos.index("function hostAndSelect"):]
    seg = seg[:seg.index("\nfunction ", 10) if "\nfunction " in seg[10:] else len(seg)]
    assert "indexOf('github:')" not in seg and "indexOf('gitcode:')" not in seg, \
        "provider 必须按任意平台前缀解析"
    dash = _read("api/static/js/pages/dashboard.js")
    assert "_repoWebHost(dashData.repo)" in dash, "issue host 必须走卡目录"
    tpl = _read("api/static/js/pages/templates.js")
    assert "_repoWebLink(" in tpl, "模板跳转必须走卡目录"
    sm = _read("api/static/js/pages/scene-market.js")
    assert "_repoWebHost(" in sm, "场景模板跳转必须走卡目录"
    # 载体存在且导出
    app = _read("api/static/js/app.js")
    assert "function _repoWebHost" in app and "window._repoWebHost" in app


def test_uc10_scene_market_platform_shipped():
    """UC10：scenes.py 场景明细 payload 下发 source_platform（前端派生 host 的前提）。"""
    src = _read("domains/mgmt/routes/scenes.py")
    assert '"source_platform": tpl.get("source_platform"' in src, \
        "场景明细必须下发模板 source_platform"


def test_uc11_cache_version_bumped():
    """UC11：index.html 对四个改动页面模块 + app.js 的 ?v= 均已 bump（只升不降）。"""
    idx = _read("api/static/index.html")
    for frag, minv in [("dashboard.js?v=", 110), ("repos.js?v=", 64),
                       ("templates.js?v=", 38), ("scene-market.js?v=", 51), ("app.js?v=", 253)]:
        m = re.search(re.escape(frag) + r"(\d+)", idx)
        assert m and int(m.group(1)) >= minv, f"{frag} 版本必须 >= {minv}"


