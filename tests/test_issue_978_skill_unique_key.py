"""Issue #978: 场景编辑技能下拉三档同名技能无法区分 — 前端源码验证测试。

验证 dashboard.js / scenes.py / help/api/06-scenes.md 中：
- _skillKeyOf 唯一键（skill_id 优先，回退 (scope, skill_name)）已落地
- 下拉选中高亮按唯一键精确命中（同名多行不再全勾选）
- _skillPickCache 缓存结构改为 唯一键 -> {name, display}（同名不互相覆盖）
- 两处 selectAgentSkill（编辑+创建）接受唯一键参数并按其命中缓存条目
- 行元素携带 data-key，选中回写 data-current 为唯一键
- 后端契约注释（scenes.py + OpenAPI description）与接口文档固化唯一键
- 保存 payload 不含唯一键字段（不改保存语义）
- dashboard.js 无语法错误（node 解析）

参考 Spec：Issue #978 issue agent 评论。
"""
import os
import subprocess

import pytest

JS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "api", "static", "js", "pages", "dashboard.js"
)
SCENES_PY = os.path.join(
    os.path.dirname(__file__), "..", "src", "api", "routes", "scenes.py"
)
SCENES_MD = os.path.join(
    os.path.dirname(__file__), "..", "help", "api", "06-scenes.md"
)


@pytest.fixture(scope="module")
def js_content():
    with open(JS_PATH, "r", encoding="utf-8") as f:
        return f.read()


class TestSkillUniqueKeyFunction:
    """_skillKeyOf 唯一键函数。"""

    def test_skill_key_of_defined(self, js_content):
        """dashboard.js 应定义 _skillKeyOf（skill_id 优先，回退 (scope, skill_name)）。"""
        assert "function _skillKeyOf(s)" in js_content
        assert "return 'id:' + id;" in js_content
        assert "return (s.scope || 'official') + ':' + (s.skill_name || '');" in js_content


class TestDropdownSelection:
    """下拉选中高亮按唯一键命中。"""

    def test_selection_uses_unique_key(self, js_content):
        """sel 判定应优先唯一键精确命中。"""
        assert "var sel = (_skillKeyOf(s) === current)" in js_content

    def test_legacy_name_fallback_requires_unique(self, js_content):
        """旧裸 skill_name 值仅在名字唯一时回退命中（同名计数守卫）。"""
        assert "_nameCount[s.skill_name] === 1" in js_content

    def test_row_carries_data_key(self, js_content):
        """行元素应携带 data-key，选中后 data-current 回写唯一键。"""
        assert 'data-key="\' + _skillEsc(_skillKeyOf(s)) + \'"' in js_content
        assert "btn.setAttribute('data-current', key || nm);" in js_content

    def test_name_count_computed(self, js_content):
        """同名计数应在渲染前计算。"""
        assert "var _nameCount = {};" in js_content


class TestSkillPickCache:
    """缓存结构以唯一键索引。"""

    def test_cache_indexed_by_unique_key(self, js_content):
        """_openSkillDropdown 缓存应以唯一键索引 {name, display}。"""
        assert "cache[_skillKeyOf(s)] = { name: s.skill_name, display: s.display_name || s.skill_name }" in js_content


class TestSelectAgentSkill:
    """两处 selectAgentSkill 接受唯一键参数。"""

    def test_signature_has_skill_key(self, js_content):
        """两处实现签名应为 (agentType, skillName, skillKey)。"""
        assert js_content.count(
            "window.selectAgentSkill = function(agentType, skillName, skillKey)"
        ) == 2

    def test_entry_lookup_by_key_then_name(self, js_content):
        """应先按 skillKey 命中缓存条目，再按 name 兜底。"""
        assert js_content.count("var entry = skillKey ? cache[skillKey] : null;") == 2
        assert js_content.count("if (cache[k] && cache[k].name === skillName)") == 2


class TestEchoAfterSave:
    """保存后回显展平映射适配新缓存结构。"""

    def test_flatten_reads_name_display(self, js_content):
        assert "if (_c[k] && _c[k].name) skillDisplayMap[_c[k].name] = _c[k].display;" in js_content


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


class TestSavePayloadUnchanged:
    """保存 payload 不含唯一键字段（不改保存语义）。"""

    def test_payload_excludes_skill_key(self, js_content):
        import re
        payload_pattern = re.compile(
            r"\{\s*id:\s*n\.id,\s*tag:\s*n\.tag,\s*skill:\s*n\.skill\s*\|\|\s*''[^}]*\}"
        )
        matches = payload_pattern.findall(js_content)
        assert len(matches) >= 2
        for i, p in enumerate(matches):
            assert "skill_key" not in p and "skill_id" not in p, (
                f"第 {i + 1} 处 nodesPayload 不应含唯一键字段"
            )


class TestJsSyntax:
    """dashboard.js 无语法错误（node 解析）。"""

    def test_no_syntax_errors(self):
        result = subprocess.run(
            ["node", "--check", JS_PATH], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
