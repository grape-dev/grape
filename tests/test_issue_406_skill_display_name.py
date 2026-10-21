"""Issue #406: 添加场景 VOD skill 选「开发者声音分析（CLI版）」后禁用 SRE，skill 显示名退化为 vod-cli — 前端源码验证测试。

验证 dashboard.js 中：
- 两处 selectAgentSkill（编辑模式 _renderSceneEditMode 内 + 创建模式 _enterCreateStandardMode 内）选中时同步写 node.skill_display_name
- 两处 toggleAgentInScene 禁用 agent 时 skill_display_name 与 skill 同清
- _renderSceneEditMode editNodes 构建含 skill_display_name（继承 node/ag）
- _renderEditFlowCards 的 skillDisplay 优先取 node.skill_display_name
- 保存 payload（nodesPayload）显式字段列表不含 skill_display_name（不改保存语义）
- dashboard.js 无语法错误（node 解析）

参考 Spec：Issue #406 评论（comment id 195000627）。
"""
import os
import subprocess

import pytest

JS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "api", "static", "js", "pages", "dashboard.js"
)


@pytest.fixture(scope="module")
def js_content():
    with open(JS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _extract_function_section(src, marker):
    """从 src 中截取 marker 起始的函数体（到下一个顶层 `};` 或 `}` 结束）。"""
    start = src.find(marker)
    assert start >= 0, f"未找到 {marker}"
    return start


class TestSelectAgentSkillWritesDisplayName:
    """selectAgentSkill 选中时同步写 node.skill_display_name。"""

    def test_select_writes_skill_display_name_from_cache(self, js_content):
        """selectAgentSkill 实现应从 _skillPickCache 取 display_name 写入 node.skill_display_name。"""
        # 两处实现（编辑 + 创建）均使用 cache[skillName] 模式
        matches = js_content.count(
            "node.skill_display_name = skillName ? (cache[skillName] || skillName) : ''"
        )
        assert matches == 2, (
            f"selectAgentSkill 应有两处实现均写 node.skill_display_name，实际 {matches}"
        )

    def test_select_sets_skill_to_skillName_or_empty(self, js_content):
        """selectAgentSkill 应将 node.skill 设为 skillName（空则清空）。"""
        assert js_content.count("node.skill = skillName || '';") == 2, (
            "两处 selectAgentSkill 应将 node.skill 设为 skillName || ''"
        )

    def test_select_uses_skill_pick_cache(self, js_content):
        """selectAgentSkill 应从 window._skillPickCache 取 cache。"""
        assert js_content.count("var cache = (window._skillPickCache || {})[agentType] || {};") == 2, (
            "两处 selectAgentSkill 应从 window._skillPickCache 取 cache"
        )

    def test_select_returns_safely_when_node_missing(self, js_content):
        """selectAgentSkill 找不到 node 时应安全返回（if (!node) return;）。"""
        # 两处实现都有 `if (!node) return;` 守卫
        select_sections = []
        for marker in ["window.selectAgentSkill = function(agentType, skillName)"]:
            start = js_content.find(marker)
            assert start >= 0, "selectAgentSkill 函数未找到"
            # 截取函数体（约 200 字符足够覆盖守卫）
            section = js_content[start:start + 400]
            select_sections.append(section)
        for i, sec in enumerate(select_sections):
            assert "if (!node) return;" in sec, f"第 {i + 1} 处 selectAgentSkill 缺少 node 守卫"


class TestToggleAgentInSceneClearsDisplayName:
    """toggleAgentInScene 禁用 agent 时 skill_display_name 与 skill 同清。"""

    def test_disable_clears_skill_display_name(self, js_content):
        """禁用分支应同时清 skill / skill_display_name / engine_runtime_ids。"""
        matches = js_content.count(
            "node.skill = ''; node.skill_display_name = ''; node.engine_runtime_ids = [];"
        )
        assert matches == 2, (
            f"两处 toggleAgentInScene 禁用分支应清 skill_display_name，实际 {matches}"
        )


class TestRenderSceneEditModeInheritsDisplayName:
    """_renderSceneEditMode editNodes 构建继承 skill_display_name。"""

    def test_editnodes_inherit_skill_display_name(self, js_content):
        """editNodes 构建应包含 skill_display_name: node.skill_display_name || ag.skill_display_name || ''。"""
        assert (
            "skill_display_name: node.skill_display_name || ag.skill_display_name || ''"
            in js_content
        ), "_renderSceneEditMode editNodes 构建应继承 skill_display_name"

    def test_editnodes_disabled_branch_has_empty_skill_display_name(self, js_content):
        """场景中不存在的 agent 分支应含 skill_display_name: ''。"""
        assert "skill: '', skill_display_name: '', mode: 'emit'" in js_content, (
            "editNodes 置灰分支应含 skill_display_name: ''"
        )


class TestCreateModeEditNodesInit:
    """创建模式初始 editNodes 含 skill_display_name: ''（字段一致性）。"""

    def test_create_mode_editnodes_init_has_skill_display_name(self, js_content):
        """_enterCreateStandardMode 初始 editNodes 应含 skill_display_name: ''。"""
        assert "skill: '', skill_display_name: '', mode: 'emit'" in js_content, (
            "创建模式初始 editNodes 应含 skill_display_name: ''"
        )


class TestRenderEditFlowCardsPrioritizesDisplayName:
    """_renderEditFlowCards 的 skillDisplay 优先取 node.skill_display_name。"""

    def test_skill_display_takes_priority(self, js_content):
        """skillDisplay 表达式应优先取 node.skill_display_name || node.skill。"""
        assert "node.skill_display_name || node.skill" in js_content, (
            "_renderEditFlowCards 的 skillDisplay 应优先取 node.skill_display_name || node.skill"
        )


class TestSavePayloadSemanticsUnchanged:
    """保存 payload 不含 skill_display_name（不改保存语义）。"""

    def test_save_payload_excludes_skill_display_name(self, js_content):
        """nodesPayload 显式字段列表不应含 skill_display_name。"""
        # 找到两处 nodesPayload 定义
        payload_starts = []
        needle = "const nodesPayload = editNodes.filter(n => n.enabled).map((n, i, arr) => {"
        idx = 0
        while True:
            pos = js_content.find(needle, idx)
            if pos < 0:
                break
            payload_starts.append(pos)
            idx = pos + 1
        assert len(payload_starts) == 2, f"应有两处 nodesPayload（编辑保存 + 创建保存），实际 {len(payload_starts)}"
        for i, start in enumerate(payload_starts):
            section = js_content[start:start + 600]
            # 截取到 return {...} 闭合
            ret_start = section.find("return {")
            assert ret_start >= 0, f"第 {i + 1} 处 nodesPayload 缺少 return 语句"
            ret_section = section[ret_start:section.find("};", ret_start) + 2]
            assert "skill: n.skill" in ret_section, f"第 {i + 1} 处 nodesPayload 应含 skill: n.skill"
            assert "skill_display_name" not in ret_section, (
                f"第 {i + 1} 处 nodesPayload 不应含 skill_display_name（不改保存语义）: {ret_section}"
            )


class TestSyntaxValid:
    """JS 语法验证。"""

    def test_no_syntax_errors(self, js_content):
        """dashboard.js 无语法错误（node 解析）。"""
        result = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(process.argv[1],'utf8')); console.log('OK');", JS_PATH],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"JS 语法错误: {result.stderr}"
        assert "OK" in result.stdout, f"JS 语法验证未通过: {result.stderr}"
