"""Scene list pagination for the project-scene page (dashboard scene center).

As repos accumulate scenes, GET /api/scenes and GET /api/scenes/with-pipeline
return everything at once and the frontend renders every card. This suite
pins the pagination contract added on feat/scene-list-pagination (Issue #501):

- /api/scenes supports page/page_size (page omitted -> full list, backward
  compatible) and always returns total
- /api/scenes/with-pipeline supports the same params so the per-card
  skill-chain enrichment stays page-aligned with the list; the engine probe
  fields (offline_nodes/sre_unbound/all_offline) are REMOVED from it and live
  in the on-demand GET /scenes/{scene_id}/engine-status endpoint
- the frontend dashboard renders a pager (bound to total) and requests
  paged data; the task-create popup probes engine status per selected scene

Testing approach: behavioral (real DB seeds + direct route calls, same
pattern as tests/test_issue_479_scene_label.py) + source-level contracts
for the frontend (pattern of tests/test_issue_372_round3_frontend.py).
Engine-probe semantics are covered separately in
tests/test_issue_404_create_task_offline_hint.py (migrated to engine-status).
"""
import os
import re

import pytest

sys_path_root = os.path.join(os.path.dirname(__file__), "..")
import sys  # noqa: E402

sys.path.insert(0, sys_path_root)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")

REPO_KEY = "gitcode:test-scene-paging/repo"


def _read_file(rel_path):
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _seed_scenes():
    """Seed 25 scenes (page_size boundary cases: 20 -> 2 pages, remainder 5)."""
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM devops_pipeline_scene WHERE repo_key = ?", (REPO_KEY,)
        )
        for i in range(1, 26):
            c.execute(
                "INSERT INTO devops_pipeline_scene"
                " (scene_id, name, display_name, repo_key, nodes_json, is_active, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, datetime('2026-01-01', ? || ' days'))",
                (f"pg-{i:03d}", f"name-{i:03d}", f"disp-{i:03d}", REPO_KEY, "[]", f"+{i-1}"),
            )
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key = ?", (REPO_KEY,))
        db.commit()


class TestScenesListPagination:
    """Behavioral contract: GET /api/scenes pagination."""

    def test_t1_01_full_list_without_params(self):
        """No page param -> full list (backward compatible), total present."""
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(user={"auth_type": "session_token"}, repo="test-scene-paging/repo")
        assert len(resp["scenes"]) == 25
        assert resp.get("total") == 25

    def test_t1_02_paged_first_page(self):
        """page=1&page_size=20 -> 20 items + total=25."""
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=1, page_size=20,
        )
        assert len(resp["scenes"]) == 20
        assert resp["total"] == 25
        ids = [s["id"] for s in resp["scenes"]]
        assert ids[0] == "pg-001" and "pg-021" not in ids

    def test_t1_03_paged_last_page(self):
        """page=2&page_size=20 -> 5 remainder items."""
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=2, page_size=20,
        )
        assert len(resp["scenes"]) == 5
        assert resp["total"] == 25

    def test_t1_04_page_beyond_range_empty(self):
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=9, page_size=20,
        )
        assert resp["scenes"] == []
        assert resp["total"] == 25


class TestWithPipelinePagination:
    """Behavioral contract: /scenes/with-pipeline stays page-aligned, probe fields removed."""

    def test_t2_01_paged_with_pipeline(self):
        """with-pipeline honors page/page_size and returns total."""
        from src.api.routes.scenes import list_scenes_with_pipeline

        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=1, page_size=10,
        )
        assert len(resp["scenes"]) == 10
        assert resp["total"] == 25
        # enrichment fields must still be present per scene
        for s in resp["scenes"]:
            assert "pipeline" in s

    def test_t2_02_full_without_params(self):
        from src.api.routes.scenes import list_scenes_with_pipeline

        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo"
        )
        assert len(resp["scenes"]) == 25
        assert resp.get("total") == 25

    def test_t2_03_engine_probe_fields_removed(self):
        """Issue #501: offline_nodes/sre_unbound/all_offline 已移出 with-pipeline。

        引擎在线状态改由按需接口 /scenes/{scene_id}/engine-status 提供，
        消费方只有需求中心弹窗（语义测试见 test_issue_404_...）。
        """
        from src.api.routes.scenes import list_scenes_with_pipeline

        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=1, page_size=5,
        )
        for s in resp["scenes"]:
            assert "offline_nodes" not in s
            assert "sre_unbound" not in s
            assert "all_offline" not in s

    def test_t2_04_engine_status_endpoint_reachable(self):
        """新按需探测接口可直达；种子场景 nodes 为空 → 降级不标记。"""
        from src.api.routes.scenes import get_scene_engine_status

        st = get_scene_engine_status(
            "pg-001", user={"auth_type": "session_token"}, repo="test-scene-paging/repo")
        assert st["scene_id"] == "pg-001"
        assert st["all_offline"] is False
        assert st["offline_nodes"] == []


class TestFrontendPager:
    """Source-level contracts for the dashboard pager."""

    def test_t3_01_frontend_requests_paged_scenes(self):
        """app.js 的场景列表请求必须携带 page 参数（分页状态驱动）。
        Issue #501 A 方案后该请求为 /scenes/with-pipeline（单一来源，兼出 _pipelines）。"""
        src = _read_file("js/app.js")
        assert re.search(
            r"/scenes/with-pipeline\?repo=' \+ encodeURIComponent\([^)]*\)[^\n]*&page='", src
        ), "前端场景列表请求未携带分页参数"
        assert "window._sceneListPage" in src, "分页页码状态缺失"
        # A 方案：loadDash 不再并发打第二个 /scenes 列表请求（同表同页双读）
        fn = src.split("const loadDash = () => {")[1].split("}).then(")[0]
        assert "/scenes?repo=" not in fn, "loadDash 内不应残留 /scenes 旧调用"

    def test_t3_02_pager_ui_rendered(self):
        """_renderSceneCards 必须渲染分页条并按跨页总数计算总页数。"""
        src = _read_file("js/pages/dashboard.js")
        assert "_renderScenePager" in src, "dashboard.js 缺少场景分页条渲染函数"
        body = src[src.find("function _renderScenePager"):]
        assert "sceneTotal" in body[:2000], "分页条未使用跨页总数 sceneTotal 计算总页数"
        assert "_sceneGotoPage" in body[:2000], "分页条缺少翻页动作"

    def test_t3_03_count_badge_uses_total(self):
        """场景数量角标应显示跨页总数（sceneTotal），非当前页条数。"""
        src = _read_file("js/pages/dashboard.js")
        m = re.search(r"scene_center_title.*?\$\{sceneTotal\}", src, re.S)
        assert m, "数量角标仍显示 scenesData.length，应显示分页 total（sceneTotal）"

    def test_t3_04_pipeline_fetch_page_aligned(self):
        """with-pipeline 拉取与 /scenes 同 page/page_size，技能链只 enrich 当前页。"""
        src = _read_file("js/pages/dashboard.js")
        assert re.search(
            r"/scenes/with-pipeline\?repo=' \+ encodeURIComponent\([^)]*\)[^\n]*&page='", src
        ), "with-pipeline 请求未携带分页参数（与 /scenes 页不对齐）"

    def test_t3_05_repo_switch_resets_page(self):
        """切换仓库时页码归位，避免带旧仓库页码拉新仓库（越界空列表）。"""
        src = _read_file("js/app.js")
        assert "_sceneListRepo" in src, "缺少仓库切换页码归位逻辑"


class TestFrontendPopupOnDemandProbe:
    """Source-level contracts: 需求中心弹窗改为选中场景时按需探测。"""

    def test_t4_01_popup_probes_on_selection(self):
        """弹窗 onchange 走 engine-status 按需探测，不再消费 with-pipeline 预标记。"""
        src = _read_file("js/pages/issues.js")
        assert "/engine-status?repo=" in src, "弹窗未调用按需引擎探测接口"
        assert re.search(
            r"sel\.onchange = function\(\) \{\s*_updateSceneOfflineWarning\(sel\)", src
        ), "onchange 未联动按需探测"

    def test_t4_02_popup_options_no_offline_tags(self):
        """下拉选项不再带预探测 ⚠️ 标记（探测时机移到选中后）。"""
        src = _read_file("js/pages/issues.js")
        start = src.find("tcCreateScene")
        popup = src[start:start + 6000]
        assert "all_offline ? ' ⚠️'" not in popup, "下拉选项仍消费 all_offline 预标记"
        assert "opt._all_offline" not in popup, "选项上仍挂离线预标记属性"

    def test_t4_03_stale_selection_guard(self):
        """异步探测返回时须校验用户没切到别的场景（stale guard）。"""
        src = _read_file("js/pages/issues.js")
        assert re.search(r"sel\.value !== sceneId", src), "缺少选中态过期保护"


class TestDeepReviewFixes:
    """Issue #501 深审补修：P1 回退误报 / P2 创建后末页可见 / P3 repo 解析一致性 / scope 字段。"""

    def test_t5_01_engine_status_unknown_scene_no_false_warning(self):
        """P1：不存在的场景 id 不得静默回退 default 流水线误报 sre_unbound。"""
        from src.api.routes.scenes import get_scene_engine_status

        st = get_scene_engine_status(
            "no-such-scene-xyz", user={"auth_type": "session_token"}, repo="test-scene-paging/repo")
        assert st["all_offline"] is False
        assert st["sre_unbound"] is False
        assert st["offline_nodes"] == []

    def test_t5_02_with_pipeline_items_carry_scope(self):
        """优化点4：with-pipeline 补 scope 字段——前端下拉 scopeTag 不再是死代码。"""
        from src.api.routes.scenes import list_scenes_with_pipeline

        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
            page=1, page_size=5)
        assert resp["scenes"], "seeded scenes expected"
        for s in resp["scenes"]:
            assert s.get("scope") == "repo"

    def test_t5_03_engine_status_uses_session_repo_resolution(self):
        """P3：engine-status 与列表同用 _resolve_scene_repo_key（#534 会话级解析）。"""
        full = os.path.join(sys_path_root, "src", "api", "routes", "scenes.py")
        with open(full, encoding="utf-8") as f:
            body = f.read()
        # get_scene_engine_status 函数体内应调用 _resolve_scene_repo_key，而非 to_repo_key 直转
        fn = body.split("def get_scene_engine_status")[1].split("def ")[0]
        assert "_resolve_scene_repo_key(repo, user)" in fn
        assert "to_repo_key(repo)" not in fn

    def test_t5_04_pager_reloads_locally(self):
        """优化点3：翻页优先走 _sceneReload 局部重载，不再整页 nav('dash')。"""
        dash = _read_file("js/pages/dashboard.js")
        assert "window._sceneReload" in dash
        assert "typeof window._sceneReload === 'function'" in dash
        appjs = _read_file("js/app.js")
        assert "window._sceneReload = loadDash" in appjs

    def test_t5_05_create_success_jumps_to_last_page(self):
        """P2：创建成功后跳末页，新卡片（id 最大在末页）立即可见。"""
        dash = _read_file("js/pages/dashboard.js")
        assert "Math.ceil(" in dash and "_sceneListPage = _last" in dash


class TestPagingIntegrity:
    """Issue #501 补齐（全流程审查）：分页数据完整性 / 边界 / 作用域隔离 行为级用例。"""

    def test_t1_05_all_pages_concat_equals_full_no_drift_no_overlap(self):
        """跨页完整性：按 page_size=7 翻完 25 条，拼接结果 == 全量列表（顺序、无缺、无重）。"""
        from src.api.routes.scenes import list_scenes

        full = list_scenes(
            user={"auth_type": "session_token"}, repo="test-scene-paging/repo")["scenes"]
        paged = []
        p = 1
        while True:
            resp = list_scenes(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
                page=p, page_size=7)
            if not resp["scenes"]:
                break
            paged.extend(resp["scenes"])
            p += 1
            assert p < 10, "防止死循环"
        assert [s["id"] for s in paged] == [s["id"] for s in full]
        assert len(paged) == len(set(s["id"] for s in paged)) == 25

    def test_t1_06_soft_deleted_rows_excluded_from_list_and_total(self):
        """软删（is_active=0）不计入列表也不计入 total——COUNT 与列表 WHERE 一致。"""
        from src.session_manager import db, db_lock
        from src.api.routes.scenes import list_scenes

        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO devops_pipeline_scene"
                " (scene_id, name, repo_key, nodes_json, is_active)"
                " VALUES ('pg-dead', 'dead', ?, '[]', 0)", (REPO_KEY,))
            db.commit()
        try:
            resp = list_scenes(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
                page=1, page_size=100)
            assert resp["total"] == 25
            assert all(s["id"] != "pg-dead" for s in resp["scenes"])
            full = list_scenes(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo")
            assert full["total"] == 25
            assert all(s["id"] != "pg-dead" for s in full["scenes"])
        finally:
            with db_lock:
                db.cursor().execute(
                    "DELETE FROM devops_pipeline_scene WHERE scene_id='pg-dead'")
                db.commit()

    def test_t1_07_repo_scope_isolation(self):
        """仓隔离：另一仓库的场景不出现在本仓列表，也不计入 total。"""
        from src.session_manager import db, db_lock
        from src.api.routes.scenes import list_scenes

        other = "gitcode:test-scene-paging/other-repo"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO devops_pipeline_scene"
                " (scene_id, name, repo_key, nodes_json, is_active)"
                " VALUES ('other-001', 'other', ?, '[]', 1)", (other,))
            db.commit()
        try:
            resp = list_scenes(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
                page=1, page_size=100)
            assert resp["total"] == 25
            assert all(s["id"] != "other-001" for s in resp["scenes"])
        finally:
            with db_lock:
                db.cursor().execute(
                    "DELETE FROM devops_pipeline_scene WHERE repo_key=?", (other,))
                db.commit()

    def test_t1_08_min_page_size_one(self):
        """page_size=1 最小边界：每页恰好 1 条，total 不变。"""
        from src.api.routes.scenes import list_scenes

        r1 = list_scenes(user={"auth_type": "session_token"},
                         repo="test-scene-paging/repo", page=1, page_size=1)
        r25 = list_scenes(user={"auth_type": "session_token"},
                          repo="test-scene-paging/repo", page=25, page_size=1)
        r26 = list_scenes(user={"auth_type": "session_token"},
                          repo="test-scene-paging/repo", page=26, page_size=1)
        assert len(r1["scenes"]) == 1 and r1["total"] == 25
        assert len(r25["scenes"]) == 1
        assert r26["scenes"] == [] and r26["total"] == 25

    def test_t2_05_paged_nodes_match_full_version(self):
        """分页 enrich 对齐：分页返回的某场景 nodes 与全量返回的同场景 nodes 完全一致。"""
        from src.session_manager import db, db_lock
        from src.api.routes.scenes import list_scenes_with_pipeline

        nodes = [{"id": "n1", "tag": "vod", "skill": "skill-a", "enabled": True}]
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE devops_pipeline_scene SET nodes_json=? WHERE scene_id='pg-003'",
                (__import__("json").dumps(nodes),))
            db.commit()
        try:
            paged = list_scenes_with_pipeline(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
                page=1, page_size=5)["scenes"]
            full = list_scenes_with_pipeline(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo")["scenes"]
            p3_paged = next(s for s in paged if s["id"] == "pg-003")
            p3_full = next(s for s in full if s["id"] == "pg-003")
            assert p3_paged["pipeline"]["nodes"] == p3_full["pipeline"]["nodes"]
            assert p3_paged["pipeline"]["nodes"][0]["tag"] == "vod"
            # skill_display_name enrich 在分页路径同样生效
            assert "skill_display_name" in p3_paged["pipeline"]["nodes"][0]
        finally:
            with db_lock:
                db.cursor().execute(
                    "UPDATE devops_pipeline_scene SET nodes_json='[]' WHERE scene_id='pg-003'")
                db.commit()


class TestFullFlowReview:
    """Issue #501 全流程审查补漏用例。"""

    def test_t3_06_empty_page_clamps_to_last(self):
        """页码越界（他人删场景后总页数收缩）自动收敛末页，防 "空列表 + 3/2 分页条"。"""
        dash = _read_file("js/pages/dashboard.js")
        assert "scenes.length === 0 && sceneTotal > 0" in dash
        assert "window._sceneGotoPage(_last)" in dash
        # 防死循环：收敛后页码 == 末页，条件不再触发（源级：跳转前有 return，且仅在 _cur > _last 时跳）
        assert "if ((window._sceneListPage || 1) > _last)" in dash


class TestSceneSearch:
    """Issue #501 增量：场景检索 q / 排序 sort（列表 + with-pipeline 同参同语义）。"""

    def test_t6_01_q_hits_name_substring(self):
        """q 命中 name 子串：name-02* 共 6 条（020-025），total 同步为命中数。"""
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(user={"auth_type": "session_token"},
                           repo="test-scene-paging/repo", q="name-02")
        assert resp["total"] == 6
        assert len(resp["scenes"]) == 6
        assert all(s["name"].startswith("name-02") for s in resp["scenes"])

    def test_t6_02_q_hits_display_name_and_description(self):
        """q 命中 display_name / description 字段。"""
        from src.api.routes.scenes import list_scenes
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_pipeline_scene SET display_name='生产发布流水线' WHERE scene_id='pg-001'")
            c.execute("UPDATE devops_pipeline_scene SET description='staging deploy chain' WHERE scene_id='pg-002'")
            db.commit()
        try:
            r1 = list_scenes(user={"auth_type": "session_token"},
                             repo="test-scene-paging/repo", q="生产发布")
            assert r1["total"] == 1 and r1["scenes"][0]["id"] == "pg-001"
            r2 = list_scenes(user={"auth_type": "session_token"},
                             repo="test-scene-paging/repo", q="staging")
            assert r2["total"] == 1 and r2["scenes"][0]["id"] == "pg-002"
        finally:
            with db_lock:
                db.cursor().execute(
                    "UPDATE devops_pipeline_scene SET display_name='disp-001' WHERE scene_id='pg-001'")
                db.cursor().execute(
                    "UPDATE devops_pipeline_scene SET description='' WHERE scene_id='pg-002'")
                db.commit()

    def test_t6_03_q_no_hit_returns_empty_with_zero_total(self):
        from src.api.routes.scenes import list_scenes

        resp = list_scenes(user={"auth_type": "session_token"},
                           repo="test-scene-paging/repo", q="zzz-no-hit")
        assert resp["total"] == 0
        assert resp["scenes"] == []

    def test_t6_04_q_with_paging_combination(self):
        """q + page 组合：命中 6 条，page_size=4 → 第 1 页 4 条、第 2 页 2 条，页内不重不漏。"""
        from src.api.routes.scenes import list_scenes

        p1 = list_scenes(user={"auth_type": "session_token"},
                         repo="test-scene-paging/repo", q="name-02", page=1, page_size=4)
        p2 = list_scenes(user={"auth_type": "session_token"},
                         repo="test-scene-paging/repo", q="name-02", page=2, page_size=4)
        p3 = list_scenes(user={"auth_type": "session_token"},
                         repo="test-scene-paging/repo", q="name-02", page=3, page_size=4)
        ids = [s["id"] for s in p1["scenes"]] + [s["id"] for s in p2["scenes"]]
        assert (len(p1["scenes"]), len(p2["scenes"])) == (4, 2)
        assert p3["scenes"] == [] and p3["total"] == 6
        assert len(set(ids)) == 6

    def test_t6_05_with_pipeline_same_q_semantics(self):
        """with-pipeline 与 /scenes 同 q 语义：同命中集、同 total。"""
        from src.api.routes.scenes import list_scenes, list_scenes_with_pipeline

        a = list_scenes(user={"auth_type": "session_token"},
                        repo="test-scene-paging/repo", q="name-02")
        b = list_scenes_with_pipeline(user={"auth_type": "session_token"},
                                      repo="test-scene-paging/repo", q="name-02")
        assert b["total"] == a["total"] == 6
        assert [s["id"] for s in b["scenes"]] == [s["id"] for s in a["scenes"]]

    def test_t6_06_sort_latest_orders_by_created_at_desc(self):
        """sort=latest：按 created_at 倒序（fixture created_at 随 id 递增 → 逆序）；默认 id 序不受影响。"""
        from src.api.routes.scenes import list_scenes

        r = list_scenes(user={"auth_type": "session_token"},
                        repo="test-scene-paging/repo", page=1, page_size=3, sort="latest")
        assert [s["id"] for s in r["scenes"]] == ["pg-025", "pg-024", "pg-023"]
        rid = list_scenes(user={"auth_type": "session_token"},
                          repo="test-scene-paging/repo", page=1, page_size=3)
        assert [s["id"] for s in rid["scenes"]] == ["pg-001", "pg-002", "pg-003"]

    def test_t6_07_q_and_sort_frontend_contracts(self):
        """前端源级契约：搜索框在场景中心卡片头部、防抖局部重载、焦点保持、无命中空态、切仓重置 q、with-pipeline 同 q。"""
        dash = _read_file("js/pages/dashboard.js")
        appjs = _read_file("js/app.js")
        # 搜索框位于 cd-h 头部（标题同视野）
        assert 'id="sceneSearch"' in dash and "cd-h" in dash
        # 防抖 300ms + 提交走局部重载
        assert "setTimeout" in dash and "300" in dash
        assert "_sceneSearchCommit" in dash and "window._sceneReload()" in dash
        # 输入即页码归位
        assert "window._sceneListPage = 1" in dash
        # 焦点保持（重渲染后光标置尾）
        assert "sceneSearchFocus" in dash and "setSelectionRange" in dash
        # 无命中空态区分"无匹配"与"暂无场景"
        assert "scene_search_no_hit" in dash and "no_scene_title" in dash
        # app.js 请求带 q、切仓重置 q
        assert "encodeURIComponent(_q)" in appjs
        assert "window._sceneListQ = ''" in appjs
        # with-pipeline 与列表同 q
        assert "encodeURIComponent(_q2)" in dash

    def test_t6_08_q_read_inside_loaddash_not_captured_stale(self):
        """全流程审查补漏：q 必须在 loadDash 调用时取值——闭包外取值会让 30s 自动刷新/
        _sceneReload 复用旧 q，静默丢搜索词（曾引入后修复）。"""
        appjs = _read_file("js/app.js")
        # loadDash 函数体内取 q（箭头函数体内部），而非函数定义之前
        fn_start = appjs.index("const loadDash = () => {")
        fn_end = appjs.index("_refreshTimer = setInterval(loadDash", fn_start)
        body = appjs[fn_start:fn_end]
        assert "window._sceneListQ" in body
        before = appjs[:fn_start]
        assert "const _q = (window._sceneListQ" not in before


class TestCacheVersionRegression:
    """缓存版本回退事故（全流程审查发现，a53e097ca 曾把 dashboard.js v=102 回退成 101，
    老用户浏览器缓存 hoist 前旧文件 → window._branchQS is not a function /
    openEngineModal is not defined）。锁定规则：本分支改过的 JS 必须携带未用过的更高版本号。"""

    def test_t7_01_dashboard_js_cache_version_above_regression(self):
        import re as _re
        full = os.path.join(_STATIC_DIR, "index.html")
        with open(full, encoding="utf-8") as f:
            body = f.read()
        m = _re.search(r"dashboard\.js\?v=(\d+)", body)
        assert m and int(m.group(1)) >= 103, "dashboard.js 缓存版本必须 ≥103（101=hoist 前旧缓存，102=hoist 提交曾用过）"

    def test_t7_02_app_and_issues_js_cache_versions_bumped(self):
        import re as _re
        full = os.path.join(_STATIC_DIR, "index.html")
        with open(full, encoding="utf-8") as f:
            body = f.read()
        a = _re.search(r"app\.js\?v=(\d+)", body)
        i = _re.search(r"issues\.js\?v=(\d+)", body)
        assert a and int(a.group(1)) >= 211, "app.js 本分支有改动（分页/检索/局部重载），版本须 ≥211"
        assert i and int(i.group(1)) >= 124, "issues.js 本分支有改动（弹窗按需探测），版本须 ≥124"


class TestDeepReviewRound4:
    """Issue #501 深审第四轮（家族扫描修复）：openEngineModal 顶层化 / 游离防抖不强拉 / 死代码清除。"""

    def test_t8_01_open_engine_modal_defined_at_top_level(self):
        """openEngineModal 必须顶层定义（_branchQS 事故同构）：渲染函数中途抛错不再导致
        引擎按钮 onclick 报 undefined。"""
        dash = _read_file("js/pages/dashboard.js")
        # 顶层定义（行首无缩进）
        import re as _re
        assert _re.search(r'^window\.openEngineModal = function', dash, _re.M)
        # 渲染函数内不再赋值（只写上下文注册表）
        assert not _re.search(r'^\s+window\.openEngineModal = function', dash, _re.M)
        # 上下文注册表机制存在且被两处渲染路径写入
        assert dash.count("window._engineModalCtx") >= 3

    def test_t8_02_stray_search_debounce_does_not_force_nav(self):
        """防抖游离提交不得强拉视图：非 dash 视图时静默更新状态，不调 app.nav('dash')。"""
        dash = _read_file("js/pages/dashboard.js")
        fn = dash.split("window._sceneSearchCommit = function")[1].split("};")[0]
        # 仅在 dash 视图才局部重载/导航；非 dash 无 app.nav 调用路径
        assert "window._currentView === 'dash'" in fn
        nav_calls = fn.count("app.nav('dash')")
        # dash 视图兜底 nav 只在 currentView==='dash' 分支内
        assert nav_calls <= 1

    def test_t8_03_dead_render_agent_cards_removed(self):
        """死代码 renderAgentCards（无调用者，内含第二处 openEngineModal 赋值与按钮）已删除。"""
        dash = _read_file("js/pages/dashboard.js")
        assert "function renderAgentCards(" not in dash
        assert "renderAgentCards" not in dash  # 连注释残留也清零


class TestSingleSourceAndMinimal:
    """Issue #501 A 方案（with-pipeline 单一来源）+ C 方案（fields=minimal 轻量下拉）。"""

    def test_t9_01_with_pipeline_items_carry_icon(self):
        """A 前置：with-pipeline 补 icon——卡片渲染可完全改吃该接口。"""
        from src.api.routes.scenes import list_scenes_with_pipeline
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_pipeline_scene SET icon='🚀' WHERE scene_id='pg-001'")
            db.commit()
        try:
            resp = list_scenes_with_pipeline(
                user={"auth_type": "session_token"}, repo="test-scene-paging/repo",
                page=1, page_size=5)
            s1 = next(s for s in resp["scenes"] if s["id"] == "pg-001")
            assert s1.get("icon") == "🚀"
            # 其余行 icon 字段恒存在（空串兜底），卡片取值不 undefined
            assert all("icon" in s for s in resp["scenes"])
        finally:
            with db_lock:
                db.cursor().execute("UPDATE devops_pipeline_scene SET icon='' WHERE scene_id='pg-001'")
                db.commit()

    def test_t9_02_scenes_fields_minimal_skips_nodes(self):
        """C：fields=minimal 时 items 不带 nodes（跳过 nodes_json 反序列化），total/字段面不变。"""
        from src.api.routes.scenes import list_scenes

        r_min = list_scenes(user={"auth_type": "session_token"},
                            repo="test-scene-paging/repo", fields="minimal")
        r_full = list_scenes(user={"auth_type": "session_token"},
                             repo="test-scene-paging/repo")
        assert r_min["total"] == r_full["total"] == 25
        assert all(s.get("nodes") in ([], None) for s in r_min["scenes"])
        assert any(s.get("nodes") for s in r_full["scenes"]) or all("nodes" in s for s in r_full["scenes"])
        # 名称类字段完整（下拉消费面：id/name/display_name/scope）
        for s in r_min["scenes"][:3]:
            assert s["id"] and s["name"] and s["display_name"] and s.get("scope")

    def test_t9_03_frontend_single_source_contracts(self):
        """A 前端契约：loadDash 只打 with-pipeline 并产出 _pipelines；dashboard 有映射不重拉。"""
        appjs = _read_file("js/app.js")
        dash = _read_file("js/pages/dashboard.js")
        assert "dashData._pipelines = {}" in appjs
        assert "dashData._pipelines[s.id] = s.pipeline" in appjs
        assert "data._pipelines && Object.keys(data._pipelines).length" in dash
        # 创建成功刷新同样单一来源
        assert "_attachScenes" in dash and "/scenes/with-pipeline?repo=" in dash

    def test_t9_04_popups_use_minimal_fields(self):
        """C 前端契约：需求中心两个下拉用 /scenes?...&fields=minimal，不再拉 with-pipeline 全量。"""
        iss = _read_file("js/pages/issues.js")
        assert iss.count("&fields=minimal") >= 2
        # 弹窗场景下拉不再调 with-pipeline（按需探测走 engine-status，不变）
        fn_create = iss.split("function openTaskCreateModal")[1].split("function openTaskCopyModal")[0] \
            if "openTaskCopyModal" in iss else iss.split("function openTaskCreateModal")[1][:8000]
        assert "/scenes/with-pipeline" not in fn_create

    def test_t9_05_dom_level_partial_refresh(self):
        """深审五：30s/翻页/搜索重载只更新卡片行/角标/分页条/hero 统计，
        不整页 innerHTML 重建（搜索框/焦点/滚动天然保持）。"""
        dash = _read_file("js/pages/dashboard.js")
        # 局部分支：DOM 存在且非空列表时走区域更新
        assert "document.getElementById('dash-scenes')" in dash
        assert "_partialTarget.innerHTML = _rows" in dash
        # 动态区域锚点 id（全量路径产出，局部路径消费）
        for anchor in ("sceneCountBadge", "scenePagerWrap", "dashStatActive", "dashStatFailed", "dashStatTotal"):
            assert f'id="{anchor}"' in dash, f"缺少锚点 {anchor}"
            assert f"getElementById('{anchor}')" in dash, f"局部刷新未消费 {anchor}"
        # 局部路径同步 _dashboardData
        fn = dash.split("// Issue #501 深审五")[1].split("let html = `<style>")[0]
        assert "window._dashboardData = data" in fn

    def test_t9_06_partial_refresh_keeps_delete_mode(self):
        """深审五补漏：删除模式挂起时 30s 局部刷新不重建卡片行（保住行上叉号/选中态）；
        视图切回（全量渲染）删除模式复位，不留脏状态。"""
        dash = _read_file("js/pages/dashboard.js")
        # 局部分支带删除模式守卫
        assert "&& !(window._dashDeleteMode)" in dash
        # 模式状态 window 级 + 全量渲染复位
        assert "window._dashDeleteMode = _deleteModeActive" in dash
        assert "if (!document.getElementById('dash-scenes')) window._dashDeleteMode = false" in dash

    def test_t9_07_delete_refresh_carries_scene_list(self):
        """深审五补漏：删除场景后的刷新必须带场景列表（原只拉 /dashboard，
        renderDashboard 拿不到 _scenes 会误渲染"暂无场景"空态页——weekly-09 原有缺陷）。"""
        dash = _read_file("js/pages/dashboard.js")
        fn = dash.split("confirmDeleteCardBtn').onclick")[1][:2600]
        assert "/scenes/with-pipeline?repo=" in fn, "删除刷新未携带场景列表"
        assert "dd._pipelines" in fn, "删除刷新未附技能链映射"
        assert "renderDashboard(dd)" in fn


class TestReviewFollowups:
    """评审遗留提醒收口：LIMIT 占位符化 + 检索 WHERE 片段单一来源。"""

    def test_t10_01_limit_offset_parameterized(self):
        """LIMIT/OFFSET 用占位符传参（与同函数其余参数化风格一致）。"""
        pc = open(os.path.join(sys_path_root, "src", "api", "pipeline_config.py"), encoding="utf-8").read()
        assert '" LIMIT ? OFFSET ?"' in pc
        assert "LIMIT {int(limit)}" not in pc, "内插 LIMIT 残留"

    def test_t10_02_search_where_single_source(self):
        """q 过滤 WHERE 片段由 _scene_search_where 单一构建，_query 与 _count 共用。"""
        pc = open(os.path.join(sys_path_root, "src", "api", "pipeline_config.py"), encoding="utf-8").read()
        assert "def _scene_search_where(" in pc
        fn_query = pc.split("def _query_scenes_from_db")[1].split("def ")[0]
        fn_count = pc.split("def _count_scenes_from_db")[1].split("def ")[0]
        assert "_scene_search_where(q" in fn_query
        assert "_scene_search_where(q" in fn_count
        # 两个函数体内不再各自拼 LIKE 片段
        assert ' OR ".join' not in fn_query and ' OR ".join' not in fn_count


class TestI18nCompleteness:
    """设计评审 D1：新功能 UI 文案 i18n 完整性——新增 key 必须双语齐备，
    不得用语义不符的既有 key 顶替（common.load_failed ≠ 场景上下文缺失提示）。"""

    REQUIRED_KEYS = [
        "scene_search_placeholder", "scene_search_no_hit",
        "scene_search_no_hit_desc", "scene_search_clear",
        "scene_engine_ctx_missing",
    ]

    def test_t11_01_new_scene_feature_keys_exist_in_both_locales(self):
        import json
        for lang in ("zh", "en"):
            p = os.path.join(_STATIC_DIR, "locales", lang, "dashboard.json")
            d = json.load(open(p, encoding="utf-8"))
            missing = [k for k in self.REQUIRED_KEYS if not d.get(k)]
            assert not missing, f"{lang} 缺 key: {missing}"

    def test_t11_02_engine_modal_toast_uses_dedicated_key(self):
        """openEngineModal 的降级 toast 用专属 key（不再借用 common.load_failed）。"""
        dash = _read_file("js/pages/dashboard.js")
        fn = dash.split("window.openEngineModal = function")[1].split("};")[0]
        assert "scene_engine_ctx_missing" in fn
        assert "common.load_failed" not in fn
