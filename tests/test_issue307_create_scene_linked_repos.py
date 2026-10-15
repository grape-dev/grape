"""Issue #307: 添加场景页补齐关联仓库配置能力 — 前端源码验证测试。

验证 dashboard.js 中：
- _enterCreateStandardMode 高级选项区域含关联仓库区块（#advLinkedReposInline + 添加按钮）
- 进入添加场景页加载 /repos/managed 填充 window._linkedRepoOptions
- _addLinkedRepoInline 为顶层函数（不在 _renderLocalSceneDetailPage 内部）
- _saveCreateScene 收集 #advLinkedReposInline .adv-kv 行并提交 linked_repos 字段
- 与修改场景页（_renderLocalSceneDetailPage）的 DOM/交互/提交逻辑对等
"""
import os

import pytest

JS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "api", "static", "js", "pages", "dashboard.js"
)


@pytest.fixture(scope="module")
def js_content():
    with open(JS_PATH, "r", encoding="utf-8") as f:
        return f.read()


class TestCreateSceneLinkedReposDOM:
    """添加场景页高级选项区域含关联仓库区块。"""

    def test_adv_linked_repos_container_in_create_mode(self, js_content):
        """#advLinkedReposInline 容器在 _enterCreateStandardMode 中出现。"""
        create_start = js_content.find("function _enterCreateStandardMode")
        assert create_start >= 0, "_enterCreateStandardMode 函数未找到"
        # _enterCreateStandardMode 以 "};\n" 结束（函数体末尾的闭包）
        create_end = js_content.find("\n};\n\n", create_start)
        create_section = js_content[create_start:create_end]
        assert 'id="advLinkedReposInline"' in create_section, (
            "添加场景页高级选项区域缺失 #advLinkedReposInline 容器"
        )

    def test_add_linked_repo_button_in_create_mode(self, js_content):
        """添加关联仓库按钮在 _enterCreateStandardMode 中出现。"""
        create_start = js_content.find("function _enterCreateStandardMode")
        create_end = js_content.find("\n};\n\n", create_start)
        create_section = js_content[create_start:create_end]
        assert 'onclick="_addLinkedRepoInline()"' in create_section, (
            "添加场景页缺失「+ 添加关联仓库」按钮"
        )

    def test_linked_repos_description_in_create_mode(self, js_content):
        """关联仓库说明文案在添加场景页出现（与修改场景页一致）。"""
        create_start = js_content.find("function _enterCreateStandardMode")
        create_end = js_content.find("\n};\n\n", create_start)
        create_section = js_content[create_start:create_end]
        assert "配置关联仓库后" in create_section, (
            "添加场景页缺失关联仓库说明文案"
        )

    def test_dom_block_count_matches_detail_page(self, js_content):
        """关联仓库 DOM 区块在添加页和修改页各出现一次（共 2 次）。"""
        assert js_content.count('id="advLinkedReposInline"') == 2, (
            "#advLinkedReposInline 应在添加页和修改页各出现一次"
        )
        assert js_content.count('onclick="_addLinkedRepoInline()"') == 2, (
            "_addLinkedRepoInline() 按钮应在添加页和修改页各出现一次"
        )


class TestCreateSceneLoadsManagedRepos:
    """进入添加场景页加载 /repos/managed 填充 _linkedRepoOptions。"""

    def test_repos_managed_loaded_in_create_mode(self, js_content):
        """_enterCreateStandardMode 内调用 /repos/managed。"""
        create_start = js_content.find("function _enterCreateStandardMode")
        create_end = js_content.find("\n};\n\n", create_start)
        create_section = js_content[create_start:create_end]
        assert "API.get('/repos/managed')" in create_section, (
            "添加场景页未加载 /repos/managed 填充 _linkedRepoOptions"
        )

    def test_repos_managed_loaded_twice(self, js_content):
        """/repos/managed 在添加页和修改页（_loadAdvancedOptionsInline）各加载一次。"""
        assert js_content.count("API.get('/repos/managed')") == 2, (
            "/repos/managed 应在添加页和详情页各加载一次"
        )


class TestAddLinkedRepoInlineTopLevel:
    """_addLinkedRepoInline 提升为顶层函数。"""

    def test_function_at_top_level(self, js_content):
        """_addLinkedRepoInline 不在 _renderLocalSceneDetailPage 内部（无缩进=顶层）。"""
        func_pos = js_content.find("window._addLinkedRepoInline = function")
        assert func_pos >= 0, "_addLinkedRepoInline 函数未找到"
        # 取该行起始位置（前一个换行后的第一个字符）
        line_start = js_content.rfind("\n", 0, func_pos) + 1
        line = js_content[line_start:func_pos]
        assert line == "", (
            f"_addLinkedRepoInline 应在顶层（无前导空格），实际前导: {repr(line)}"
        )

    def test_not_inside_render_detail(self, js_content):
        """_addLinkedRepoInline 不在 _renderLocalSceneDetailPage 函数体内。"""
        render_start = js_content.find("function _renderLocalSceneDetailPage")
        render_end = js_content.find("\n};", render_start)
        render_section = js_content[render_start:render_end]
        assert "window._addLinkedRepoInline = function" not in render_section, (
            "_addLinkedRepoInline 仍定义在 _renderLocalSceneDetailPage 内部，未提升为顶层"
        )

    def test_options_and_cache_initialized_top_level(self, js_content):
        """_linkedRepoOptions 和 _linkedRepoBranchCache 在顶层初始化。"""
        func_pos = js_content.find("window._addLinkedRepoInline = function")
        before = js_content[:func_pos]
        assert "window._linkedRepoOptions = [];" in before, (
            "_linkedRepoOptions 未在顶层初始化"
        )
        assert "window._linkedRepoBranchCache = {};" in before, (
            "_linkedRepoBranchCache 未在顶层初始化"
        )


class TestSaveCreateSceneCollectsLinkedRepos:
    """_saveCreateScene 收集并提交 linked_repos 字段。"""

    def test_collects_from_adv_linked_repos(self, js_content):
        """_saveCreateScene 从 #advLinkedReposInline .adv-kv 收集关联仓库。"""
        save_start = js_content.find("window._saveCreateScene = function")
        assert save_start >= 0, "_saveCreateScene 函数未找到"
        save_end = js_content.find("}).catch(e =>", save_start)
        save_section = js_content[save_start:save_end]
        assert "#advLinkedReposInline .adv-kv" in save_section, (
            "_saveCreateScene 未从 #advLinkedReposInline .adv-kv 收集关联仓库"
        )

    def test_pushes_repo_key_and_branch(self, js_content):
        """收集逻辑构造 {repo_key, branch} 对象。"""
        save_start = js_content.find("window._saveCreateScene = function")
        save_end = js_content.find("}).catch(e =>", save_start)
        save_section = js_content[save_start:save_end]
        assert "linkedRepos.push" in save_section, (
            "_saveCreateScene 未 push 关联仓库到列表"
        )
        assert "repo_key:" in save_section, "收集对象缺 repo_key 字段"
        assert "branch:" in save_section, "收集对象缺 branch 字段"

    def test_linked_repos_in_put_payload(self, js_content):
        """PUT /scenes/{id}/advanced-options payload 含 linked_repos 字段。"""
        save_start = js_content.find("window._saveCreateScene = function")
        save_end = js_content.find("}).catch(e =>", save_start)
        save_section = js_content[save_start:save_end]
        assert "linked_repos: linkedRepos" in save_section, (
            "_saveCreateScene 的 PUT payload 未包含 linked_repos 字段"
        )

    def test_adv_promise_includes_linked_repos_length(self, js_content):
        """advPromise 触发条件含 linkedRepos.length（空数组不影响原有流程）。"""
        save_start = js_content.find("window._saveCreateScene = function")
        save_end = js_content.find("}).catch(e =>", save_start)
        save_section = js_content[save_start:save_end]
        assert "linkedRepos.length" in save_section, (
            "advPromise 条件未包含 linkedRepos.length，空关联仓库列表可能误触发保存"
        )

    def test_empty_linked_repos_preserves_flow(self, js_content):
        """未添加关联仓库行时 linkedRepos 为空数组，不影响原有创建流程。"""
        save_start = js_content.find("window._saveCreateScene = function")
        save_end = js_content.find("}).catch(e =>", save_start)
        save_section = js_content[save_start:save_end]
        # 空数组 push 不发生 → linkedRepos.length == 0 → 不改变原有条件逻辑
        assert "var linkedRepos = [];" in save_section, (
            "linkedRepos 未初始化为空数组"
        )


class TestDetailPageUnchanged:
    """修改场景页原有功能不受影响。"""

    def test_detail_page_save_still_collects_linked_repos(self, js_content):
        """_saveAdvancedOptionsInline 仍收集关联仓库（未被破坏）。"""
        save_adv_start = js_content.find("window._saveAdvancedOptionsInline = function")
        assert save_adv_start >= 0, "_saveAdvancedOptionsInline 函数未找到"
        save_adv_end = js_content.find("\n  };\n", save_adv_start)
        save_adv_section = js_content[save_adv_start:save_adv_end]
        assert "#advLinkedReposInline .adv-kv" in save_adv_section, (
            "_saveAdvancedOptionsInline 关联仓库收集逻辑丢失"
        )
        assert "linked_repos: linkedRepos" in save_adv_section, (
            "_saveAdvancedOptionsInline PUT payload 的 linked_repos 字段丢失"
        )

    def test_detail_page_dom_intact(self, js_content):
        """修改场景页关联仓库 DOM 区块完整。"""
        render_start = js_content.find("function _renderLocalSceneDetailPage")
        render_end = js_content.find("\n};", render_start)
        render_section = js_content[render_start:render_end]
        assert 'id="advLinkedReposInline"' in render_section, (
            "修改场景页 #advLinkedReposInline 容器丢失"
        )
        assert 'onclick="_addLinkedRepoInline()"' in render_section, (
            "修改场景页「+ 添加关联仓库」按钮丢失"
        )

    def test_load_advanced_options_intact(self, js_content):
        """_loadAdvancedOptionsInline 仍加载 /repos/managed 并回填关联仓库。"""
        load_start = js_content.find("function _loadAdvancedOptionsInline")
        assert load_start >= 0, "_loadAdvancedOptionsInline 函数未找到"
        load_end = js_content.find("\n}\n", load_start)
        load_section = js_content[load_start:load_end]
        assert "API.get('/repos/managed')" in load_section, (
            "_loadAdvancedOptionsInline 未加载 /repos/managed"
        )
        assert "window._addLinkedRepoInline" in load_section, (
            "_loadAdvancedOptionsInline 未回填关联仓库"
        )


class TestSyntaxValid:
    """JS 语法验证。"""

    def test_no_syntax_errors(self, js_content):
        """dashboard.js 无语法错误（node 解析）。"""
        import subprocess
        result = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(process.argv[1],'utf8')); console.log('OK');", JS_PATH],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"JS 语法错误: {result.stderr}"
        assert "OK" in result.stdout, f"JS 语法验证未通过: {result.stderr}"
