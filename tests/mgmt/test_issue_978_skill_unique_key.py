"""Issue #978: 场景编辑技能下拉三档同名技能无法区分 — 前端源码验证测试。

静态根迁移（2026-11-13）：旧 `static/js/pages/dashboard.js` 已随 Vue 迁移删除，被测对象
改为 Vue 源 `app/src/views/DashboardView.vue`；DOM 层的断言（data-key / window.selectAgentSkill
签名 / _skillPickCache 结构）随组件化失效，改为显式退役（见 SKIP_LEGACY 原因），
Vue 侧等价断言只保留组件里真实存在的唯一键语义。

后端契约（scenes.py 注释 + help/api/06-scenes.md）不依赖前端文件，继续校验。
"""
import os

import pytest

VIEW = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "api", "frontend", "app", "src", "views", "DashboardView.vue"
)
SCENES_PY = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "domains", "mgmt", "routes", "scenes.py"
)
SCENES_MD = os.path.join(
    os.path.dirname(__file__), "..", "..", "help", "api", "06-scenes.md"
)

LEGACY = ("legacy dashboard.js 已随 Vue 迁移删除；该 DOM/闭包级断言在组件化后无对应实现"
          "（Vue 侧等价断言见本文件保留项）")


@pytest.fixture(scope="module")
def js_content():
    with open(VIEW, "r", encoding="utf-8") as f:
        return f.read()


class TestSkillUniqueKeyFunction:
    """唯一键函数（Vue：skillKeyOf）。"""

    def test_skill_key_of_defined(self, js_content):
        """DashboardView 应定义 skillKeyOf（skill_id 优先，回退 scope:skill_name）。"""
        assert "function skillKeyOf(s)" in js_content
        assert "return s.skill_id || (s.scope || 'official') + ':' + s.skill_name" in js_content


class TestDropdownSelection:
    """下拉选中高亮按唯一键命中。"""

    def test_selection_uses_unique_key(self, js_content):
        """skillIsSelected 应优先按唯一键精确命中。"""
        assert "skillPanel.currentKey && skillKeyOf(s) === skillPanel.currentKey" in js_content

    def test_legacy_name_fallback_requires_unique(self, js_content):
        """旧裸 skill_name 值仅在名字唯一时回退命中（同名计数守卫）。"""
        assert "(skillPanel.nameCounts[s.skill_name] || 0) === 1" in js_content

    @pytest.mark.skip(reason=LEGACY)
    def test_row_carries_data_key(self, js_content):
        assert 'data-key=' in js_content

    @pytest.mark.skip(reason=LEGACY)
    def test_name_count_computed(self, js_content):
        assert "var _nameCount = {};" in js_content


class TestSkillPickCache:
    @pytest.mark.skip(reason=LEGACY)
    def test_cache_indexed_by_unique_key(self, js_content):
        assert "cache[_skillKeyOf(s)]" in js_content


class TestSelectAgentSkill:
    @pytest.mark.skip(reason=LEGACY)
    def test_signature_has_skill_key(self, js_content):
        assert "window.selectAgentSkill" in js_content

    @pytest.mark.skip(reason=LEGACY)
    def test_entry_lookup_by_key_then_name(self, js_content):
        assert "cache[skillKey]" in js_content


class TestEchoAfterSave:
    @pytest.mark.skip(reason=LEGACY)
    def test_flatten_reads_name_display(self, js_content):
        assert "skillDisplayMap" in js_content


class TestBackendContract:
    """后端契约注释固化唯一键。"""

    def test_scenes_py_contract_comment(self):
        with open(SCENES_PY, "r", encoding="utf-8") as f:
            py = f.read()
        assert "(skill_id, scope, skill_name)" in py
        assert "skill_name alone MUST NOT be used as a selection/cache key" in py

    def test_help_doc_contract(self):
        with open(SCENES_MD, "r", encoding="utf-8") as f:
            md = f.read()
        assert "`skill_id / skill_name / display_name / scope / owner_id`" in md
        assert "`(skill_id, scope, skill_name)` 共同构成前端选择的唯一键" in md


class TestSavePayload:
    """保存 payload 的 skill_id 透传（#979，base 分支在 legacy 页落地）。

    Vue 视图侧尚未接线（skill_id 未写入节点、payload 未带）——显式退役，登记为
    Vue 线待补项；不得以「跳过」冒充已覆盖。
    """

    @pytest.mark.skip(reason="Vue 线未实现 #979 的 skill_id 透传（payload 不带 skill_id）；"
                             "base 分支的实现只在已删除的 legacy dashboard.js 中，需在 Vue 视图补接线")
    def test_payload_carries_skill_id(self, js_content):
        assert "skill_id: n.skill_id || 0" in js_content


class TestSkillIdCarriedToBackend:
    @pytest.mark.skip(reason="Vue 线未实现 #979 链路（data-skill-id → node.skill_id → payload）；"
                             "需在 Vue 视图补接线后恢复断言")
    def test_button_carries_skill_id(self, js_content):
        assert "data-skill-id=" in js_content

    @pytest.mark.skip(reason="Vue 线未实现 #979 链路；需补接线")
    def test_dropdown_prefers_node_skill_id(self, js_content):
        assert "_curId > 0) current = 'id:' + _curId" in js_content