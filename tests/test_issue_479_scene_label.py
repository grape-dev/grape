"""Issue #479: 流水线页场景列写死标签覆盖后端真实场景名——修复验证测试。

覆盖：
- P0 修复一: pipeline.js 场景列不再使用写死的 _sceneLabels 映射表，
  改为后端返回的 p.scene_name 优先、原始 scene_id 兜底
- P0 修复二: locales/{zh,en}/pipeline.json 中已无引用的 scene_* 硬编码文案被清理
- 回归保障: 其他页面仍在引用的 i18n 键（tasks.scene_default_badge 等）不受影响

测试方式：源码检查（inspect source）。前端 JS 无法直接在 pytest 中执行，
且本修复为纯前端展示层改动（后端 get_pipeline 的 scene_name 字段已由
tests/test_issue_239_scene_display_name.py 覆盖），故用源码断言验证。
"""
import json
import os
import re

import pytest

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")


def _read_file(rel_path):
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


class TestSceneLabelPriority:
    """T1: 场景列取值顺序——scene_name 优先，写死表移除。"""

    def test_t1_01_no_hardcoded_scene_label_map(self):
        """pipeline.js 中不应再存在 _sceneLabels 写死映射表。"""
        src = _read_file("js/pages/pipeline.js")
        assert "_sceneLabels" not in src, (
            "pipeline.js 仍存在写死的场景标签映射表 _sceneLabels，"
            "它会覆盖后端按 (scene_id, repo_key) 解析出的真实场景名（Issue #479 根因）"
        )

    def test_t1_02_scene_name_takes_priority(self):
        """场景列取值必须为 p.scene_name || _sceneId。"""
        src = _read_file("js/pages/pipeline.js")
        # 与修复实现精确匹配：scene_name 优先，scene_id 兜底
        pattern = r"const sceneLabel = _sceneId \? \(p\.scene_name \|\| _sceneId\) : '—';"
        assert re.search(pattern, src), (
            "场景列 sceneLabel 的取值顺序应为 (p.scene_name || _sceneId)，"
            "保证后端真实场景名优先、仅在缺失时显示原始 ID"
        )

    def test_t1_03_no_other_hardcoded_scene_label_lookup(self):
        """pipeline.js 中不应残留任何 scene_id→中文/英文标签的写死查找（注释除外）。"""
        src = _read_file("js/pages/pipeline.js")
        # 去掉 // 行注释与 /* */ 块注释，只检查可执行代码
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for key in ("通用开发", "云原生", "IaC", "Agent开发", "Skill开发"):
            assert key not in code, f"pipeline.js 可执行代码中残留写死场景标签: {key}"


class TestLocaleCleanup:
    """T2: 清理 pipeline.json 中失去引用的 scene_* 文案。"""

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_t2_01_pipeline_locale_no_unused_scene_keys(self, lang):
        """locales/{lang}/pipeline.json 不应再包含无引用的 scene_* 键。"""
        locale = json.loads(_read_file(f"locales/{lang}/pipeline.json"))
        for key in ("scene_default", "scene_cloud_native", "scene_iac",
                    "scene_agent_dev", "scene_skill_dev", "scene_api_skill_dev",
                    "scene_blog_publish"):
            assert key not in locale, (
                f"locales/{lang}/pipeline.json 中 {key} 已无代码引用（Issue #479 清理项）"
            )

    def test_t2_02_pipeline_js_no_scene_i18n_reference(self):
        """pipeline.js 不应再引用 pipeline.scene_* i18n 键。"""
        src = _read_file("js/pages/pipeline.js")
        assert "pipeline.scene_" not in src, "pipeline.js 仍引用已删除的 pipeline.scene_* i18n 键"

    def test_t2_03_other_pages_locale_keys_untouched(self):
        """回归保障：其他页面引用的同名 i18n 键（tasks/dashboard）不受影响。"""
        for lang in ("zh", "en"):
            tasks = json.loads(_read_file(f"locales/{lang}/tasks.json"))
            assert "scene_default_badge" in tasks, (
                f"locales/{lang}/tasks.json 的 scene_default_badge 被 Issue #479 清理误删"
            )

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_t2_04_dashboard_locale_no_unused_scene_keys(self, lang):
        """locales/{lang}/dashboard.json 的 scene_* 死键清理（家族扫描补充项）。

        dashboard 页场景名走后端 display_name + 图标映射（_SCENE_ICONS），
        不使用 scene_default/scene_cloud_native 等标签键——这些键无代码引用，
        属 #479 同族写死文案，遗留即误导。
        """
        locale = json.loads(_read_file(f"locales/{lang}/dashboard.json"))
        for key in ("scene_default", "scene_cloud_native", "scene_iac",
                    "scene_agent_dev", "scene_skill_dev", "scene_api_skill_dev",
                    "scene_blog_publish"):
            assert key not in locale, (
                f"locales/{lang}/dashboard.json 中 {key} 已无代码引用（#479 家族扫描清理项）"
            )


class TestBackendSceneNameContract:
    """T3/UC7: 行为契约——改名场景下流水线列表接口必须返回真实 scene_name。

    模拟 issue #479 实际场景：仓库 default 场景被改名（如「【开放能力】SDK」），
    种 events 行触发流水线，直接调 get_pipeline 路由（session_token 作用域
    跳过仓库权限校验），断言响应 scene_name 为改名后的 display_name。
    本用例是前端 scene_name 优先修复的数据来源保障。
    """

    REPO_KEY = "gitcode:test-479-scene-label/repo"
    RENAMED = "【测试】开放能力SDK"

    @pytest.fixture(autouse=True)
    def _seed_and_cleanup(self):
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 种仓库级 default 场景，display_name 改名（模拟 S1 场景）
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene"
                " (scene_id, name, display_name, repo_key, nodes_json, is_active)"
                " VALUES ('default', ?, ?, ?, '[]', 1)",
                (self.RENAMED, self.RENAMED, self.REPO_KEY),
            )
            # 种一条事件驱动流水线（scene_id=default，#479 实际案例形态）；
            # agent_type=vod 保证事件挂到节点 → latest_event 必有值（t3_02 硬断言依赖）
            c.execute(
                "INSERT INTO events (repo_key, event_type, issue_id, title, status, scene_id, agent_type)"
                " VALUES (?, 'issue', '#479', 'UC7 scene_name contract', 'done', 'default', 'vod')",
                (self.REPO_KEY,),
            )
            db.commit()
        yield
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key = ?", (self.REPO_KEY,))
            c.execute(
                "DELETE FROM events WHERE repo_key = ? AND issue_id = '#479'",
                (self.REPO_KEY,),
            )
            db.commit()

    def test_t3_01_renamed_default_scene_returns_real_name(self):
        """UC7: default 场景改名后，get_pipeline 响应 scene_name 必须是改名后的名称。"""
        from src.api.pipeline_config import invalidate_scene_cache
        from src.api.routes.pipeline import get_pipeline

        invalidate_scene_cache()  # 清 60s 进程级场景缓存，避免与库内其他用例互扰
        raw = get_pipeline(
            "test-479-scene-label", "repo", user={"auth_type": "session_token"}
        )
        pipes = []
        for key in ("active", "history"):
            v = raw.get(key)
            if isinstance(v, dict):
                pipes.append(v)
            elif isinstance(v, list):
                pipes.extend(v)
        assert pipes, "种入的 #479 事件未驱动出流水线条目"
        p = next((x for x in pipes if x.get("issue_id") == "#479"), pipes[0])
        assert p.get("scene_id") == "default"
        assert p.get("scene_name") == self.RENAMED, (
            "后端未返回改名后的真实场景名——前端 scene_name 优先修复的数据来源被破坏"
        )
    def test_t3_02_cross_scene_handoff_badge_contract(self):
        """UC8: 跨场景移交徽章——latest_event 必须携带 scene_id + 解析后的 scene_name。

        根因链（#479 家族）：webhook 写 cross_scene_handoff 事件带
        scene_id=target_scene_id；compute_node_statuses 塞原始事件 dict；
        但 routes/pipeline.py 白名单组装 latest_event 时丢弃了 scene_id，
        前端 _renderCrossSceneBadge 读 ev.scene_id 恒为 undefined，
        徽章永远只显示「跨场景移交」不带目标场景名。
        """
        from src.api.pipeline_config import invalidate_scene_cache
        from src.api.routes.pipeline import get_pipeline

        invalidate_scene_cache()
        raw = get_pipeline(
            "test-479-scene-label", "repo", user={"auth_type": "session_token"}
        )
        pipes = []
        for key in ("active", "history"):
            v = raw.get(key)
            if isinstance(v, dict):
                pipes.append(v)
            elif isinstance(v, list):
                pipes.extend(v)
        assert pipes
        p = pipes[0]
        # 种入的事件带 agent_type=vod → 必挂到节点，latest_event 必有值（硬断言）
        stages = p.get("stages") or []
        ev = next((s.get("latest_event") for s in stages
                   if s.get("latest_event")), None)
        assert ev is not None, "种入的事件未挂到任何节点，latest_event 缺失（软断言升级为硬断言）"
        assert ev.get("scene_id") == "default", "latest_event 丢失事件级 scene_id"
        assert ev.get("scene_name") == self.RENAMED, (
            "latest_event.scene_name 未按 (scene_id, repo_key) 解析真实场景名"
        )

    def test_t3_03_frontend_badge_prefers_scene_name(self):
        """前端 _renderCrossSceneBadge 取值必须 scene_name 优先（源码契约）。"""
        src = _read_file("js/pages/pipeline.js")
        pattern = r"crossSceneName = ev\.scene_name \|\| ev\.scene_id \|\| '';"
        assert re.search(pattern, src), (
            "_renderCrossSceneBadge 应取 ev.scene_name || ev.scene_id（真实场景名优先）"
        )

    def test_t3_04_autopilot_scene_dropdown_prefers_display_name(self):
        """UC9: autopilot 派发弹窗场景下拉必须 display_name 优先（源码契约）。

        后端 get_scenes 返回 {id, name, display_name}，修复前下拉只显示
        s.name || s.id，display_name 与 name 不同时与其他页面展示不一致。
        """
        src = _read_file("js/pages/autopilot.js")
        pattern = r"_esc\(s\.display_name \|\| s\.name \|\| s\.id \|\| ''\)"
        assert re.search(pattern, src), (
            "autopilot 派发弹窗场景下拉应优先显示 display_name（与其他页面一致）"
        )
        # value 必须仍是场景 ID（派发语义不能被展示名污染）
        pattern_val = r"'option value=\"' \+ _esc\(s\.id \|\| s\.scene_id \|\| ''\)"
        assert re.search(re.escape("_esc(s.id || s.scene_id || '')"), src), (
            "下拉 value 必须保持场景 ID（展示名仅用于显示）"
        )

    def test_t3_05_get_scenes_returns_display_name(self):
        """UC9 数据来源：get_scenes 响应必须携带 display_name（行为契约）。"""
        from src.api.pipeline_config import get_scenes, invalidate_scene_cache
        from src.session_manager import db, db_lock

        invalidate_scene_cache()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene"
                " (scene_id, name, display_name, repo_key, nodes_json, is_active)"
                " VALUES ('ap-479', 'ap-name-479', 'ap-display-479', ?, '[]', 1)",
                (self.REPO_KEY,),
            )
            db.commit()
        try:
            scenes = get_scenes(repo_key=self.REPO_KEY)
            ap = next((s for s in scenes if s.get("id") == "ap-479"), None)
            assert ap is not None, "种入的 ap-479 场景未出现在 get_scenes 结果中"
            assert ap.get("display_name") == "ap-display-479", (
                "get_scenes 未返回 display_name——autopilot 下拉修复的数据来源缺失"
            )
        finally:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "DELETE FROM devops_pipeline_scene WHERE repo_key = ? AND scene_id = 'ap-479'",
                    (self.REPO_KEY,),
                )
                db.commit()
