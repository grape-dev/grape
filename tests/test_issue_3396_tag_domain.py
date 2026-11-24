"""Issue #3396 改动②：chat 进角色标签值域——前后端值域对账扫描 + 折叠规则测试。

四处联动锚点（漏一处即行为漂移；arch 线前端已迁 Vue，锚点落在 RuntimesView.vue）：
- 后端互斥规则：src/domains/runtime/routes/runtime.py AGENT_TAGS / _apply_tag_rules
- 前端值域：RuntimesView.vue RT_TAG_OPTIONS / RT_TAG_COLORS / rtTagLabel
- 前端全选折叠：RuntimesView.vue onEdTagChange（6 角色全选 → all）
- 前端场景默认角色表：RuntimesView.vue sceneAvailableRoles
"""
import re
from pathlib import Path

import pytest

from src.domains.runtime.routes.runtime import AGENT_TAGS, _apply_tag_rules
from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES

_RT_PATH = (Path(__file__).resolve().parent.parent
            / "src" / "domains" / "runtime" / "routes" / "runtime.py")
_LINK_JS = (Path(__file__).resolve().parent.parent
            / "src" / "api" / "frontend" / "app" / "src" / "views" / "runtime-tag-link.js")
_ZH_RUNTIMES = (Path(__file__).resolve().parent.parent
                / "src" / "api" / "frontend" / "shell" / "locales" / "zh" / "runtimes.json")

_RT_VUE = (Path(__file__).resolve().parent.parent
           / "src" / "api" / "frontend" / "app" / "src" / "views" / "RuntimesView.vue")


@pytest.fixture(scope="module")
def _vue():
    return _RT_VUE.read_text(encoding="utf-8")


def _extract_list(src: str, var: str) -> list:
    m = re.search(var + r"\s*=\s*\[([^\]]*)\]", src)
    assert m, f"{var} not found in {_RT_VUE.name}"
    return [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", m.group(1))]


class TestBackendTagRules:
    def test_chat_in_agent_tags(self):
        assert "chat" in AGENT_TAGS
        assert AGENT_TAGS == {"vod", "issue", "coding", "qa", "sre", "chat"}

    def test_agent_tags_single_source(self):
        """值域不得有第二份字面量：AGENT_TAGS 必须是 engine_select.KNOWN_AGENT_ROLES 本体。"""
        assert AGENT_TAGS is KNOWN_AGENT_ROLES, "AGENT_TAGS 与 KNOWN_AGENT_ROLES 已分叉"

    def test_six_roles_collapse_to_all(self):
        # A3：6 角色全选自动折叠为全能
        assert _apply_tag_rules(["vod", "issue", "coding", "qa", "sre", "chat"]) == ["all"]

    def test_five_roles_not_collapsed(self):
        # 5 角色全选（缺 chat）不再折叠
        assert _apply_tag_rules(["vod", "issue", "coding", "qa", "sre"]) == \
            ["coding", "issue", "qa", "sre", "vod"]

    def test_all_mutually_exclusive_with_roles(self):
        assert _apply_tag_rules(["all", "chat"]) == ["chat"]
        assert _apply_tag_rules(["all", "chat", "sre"]) == ["chat", "sre"]

    def test_chat_saved_and_roundtrip(self):
        assert _apply_tag_rules(["chat"]) == ["chat"]
        # 6 角色折叠后保存/回读不再二次展开（all 与角色互斥）
        assert _apply_tag_rules(["all"]) == ["all"]

    def test_empty_allowed_no_auto_all(self):
        # 允许空标签（不自动回全能；空标签 fail-closed 由守卫侧保证）
        assert _apply_tag_rules([]) == []


class TestFrontendDomainReconciliation:
    """对账扫描：前后端值域四处一致（含 chat）。"""

    def test_rt_tag_options_matches_backend(self, _vue):
        opts = _extract_list(_vue, "const RT_TAG_OPTIONS")
        assert "chat" in opts
        assert set(opts) == AGENT_TAGS | {"all"}

    def test_on_change_agent_cbs_matches_backend(self, _vue):
        # 角色全集收敛到 views/runtime-tag-link.js（联动规则纯函数模块），
        # Vue 只做委托；行为由 tests/js/test_issue_3396_engine_tag_linkage.js 执行验证。
        link = _LINK_JS.read_text(encoding="utf-8")
        m = re.search(r"export const ALL_ROLES = \[([^\]]+)\]", link)
        assert m, "runtime-tag-link.js 里应有 ALL_ROLES"
        cbs = re.findall(r"'([^']+)'", m.group(1))
        assert set(cbs) == AGENT_TAGS, f"ALL_ROLES {cbs} != backend AGENT_TAGS {AGENT_TAGS}"
        assert "export function toPayload" in link
        assert "const ALL_ROLES = [" not in _vue, "Vue 不得再内联角色全集"

    def test_scene_default_roles_contain_chat(self, _vue):
        m = re.search(r"sceneTags\.length === 0\)\s*\n\s*return \[([^\]]+)\]", _vue)
        assert m, "sceneAvailableRoles default list not found"
        roles = re.findall(r"'([^']+)'", m.group(1))
        assert set(roles) == AGENT_TAGS | {"all"}

    def test_tag_colors_and_labels_cover_domain(self, _vue):
        m = re.search(r"const RT_TAG_COLORS = \{([^}]*)\}", _vue)
        assert m, "RT_TAG_COLORS not found"
        # 取值可以是字面色('#..')或设计令牌('var(--rtv-tg-*)')：基线 UI 令牌化后
        # RuntimesView 的车道色改为 CSS 变量，键集对账不受影响。
        colors = re.findall(r"(\w+):\s*'(?:#|var\(--rtv-tg-)", m.group(1))
        for tag in AGENT_TAGS | {"all"}:
            assert tag in colors, f"RT_TAG_COLORS missing {tag}"
        m2 = re.search(r"function rtTagLabel\(tg\)[^\{]*\{.*?\{([^}]*)\}\[tg\]", _vue, re.S)
        assert m2, "rtTagLabel map not found"
        labels = re.findall(r"(\w+):", m2.group(1))
        assert "chat" in labels, "rtTagLabel missing chat"

    def test_domain_within_known_agent_roles(self):
        # 值域必须是已知节点角色全集的子集（角色枚举以 KNOWN_AGENT_ROLES 为准，Issue #955）
        assert AGENT_TAGS <= KNOWN_AGENT_ROLES


# ── Issue #3396 复审（2026-11-20）：派生行保护 + 全能↔角色联动 + 文案 ──────


class TestDerivedRowsSurviveTagWrite:
    """PUT 保存角色标签时不得删掉冒号派生行（engine:/scene:/scope:）。

    缺陷：原实现 `DELETE FROM engine_capabilities WHERE runtime_id=?` 无条件清空，
    把 sync_derived_agent_tags 维护的派生行一并删掉——要等下一次心跳重派生，
    窗口内按 scene:/scope: 过滤引擎的查询会漏掉这台机器。
    """

    def test_delete_statement_keeps_colon_namespace(self):
        src = _RT_PATH.read_text(encoding="utf-8")
        m = re.search(r'def set_runtime_tags.*?(?=\n@router\.)', src, re.S)
        assert m, "set_runtime_tags 未找到"
        body = m.group(0)
        deletes = [ln for ln in body.splitlines() if "DELETE FROM engine_capabilities" in ln]
        assert deletes, "set_runtime_tags 里应有 engine_capabilities 的清理语句"
        # 语句可能换行，取语句及其后一行一起判
        joined = body.replace("\n", " ")
        assert re.search(
            r"DELETE FROM engine_capabilities\s+WHERE runtime_id=\?\s+AND tag NOT LIKE '%:%'", joined
        ), "PUT 的 DELETE 必须限定平铺行（AND tag NOT LIKE '%:%'），否则会清掉派生行"

    def test_derived_rows_survive_same_delete(self, tmp_path):
        import sqlite3

        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE engine_capabilities (runtime_id INT, tag TEXT, enabled INT)")
        con.executemany(
            "INSERT INTO engine_capabilities VALUES (?,?,1)",
            [(7, "chat"), (7, "sre"), (7, "engine:hermes"), (7, "scene:default"), (7, "scope:user")],
        )
        con.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag NOT LIKE '%:%'", (7,))
        left = sorted(r[0] for r in con.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=7"))
        assert left == ["engine:hermes", "scene:default", "scope:user"], left


class TestFrontendAllRoleLinkage:
    """勾「全能」→ 可勾角色全勾；全勾 → 自动全能；取消任一 → 全能取消；
    保存时全勾只下发 ['all']。行为级断言在 node 用例里执行纯函数；
    这里守「规则只存在一份、Vue 必须委托」的结构约束。"""

    def _link(self):
        return _LINK_JS.read_text(encoding="utf-8")

    def test_rules_live_in_pure_module(self):
        link = self._link()
        for fn in ("isAllChecked", "isChecked", "toggle", "toPayload", "visibleRoles", "initialRoles"):
            assert f"export function {fn}" in link, f"缺少纯函数 {fn}"

    def test_visible_roles_basis_not_fixed_six(self):
        link = self._link()
        assert "export function visibleRoles" in link
        body = re.search(r"export function isAllChecked.*?\n\}", link, re.S).group(0)
        assert "visibleRoles(visible)" in body, "全选判定必须基于可勾集合，不能写死六档"

    def test_vue_delegates_and_does_not_inline_rules(self, _vue):
        assert "from './runtime-tag-link.js'" in _vue
        assert re.search(r"toggleRoleTag\(edAgentTags\.value", _vue), "勾选必须走 toggleRoleTag"
        assert re.search(r"toPayload\(edAgentTags\.value", _vue), "保存必须走 toPayload"
        assert "agents.length === 6" not in _vue, "不得再写死六档折叠判定"


class TestSingleDescWording:
    """用户 2026-11-20 裁定：角色描述去掉「仅」字。"""

    def test_zh_single_desc_without_jin(self):
        import json

        zh = json.loads(_ZH_RUNTIMES.read_text(encoding="utf-8"))
        val = zh["tag"]["single_desc"]
        assert val == "{{label}} 角色", val
        assert "仅" not in val

    def test_domain_single_source_both_writers(self):
        """M3：两处写链路必须同源 —— settings 单标签端点接受集合 == 角色值域 - all。"""
        from src.domains.runtime.public import role_tag_domain
        import inspect
        from src.domains.mgmt.routes import settings as S
        src = inspect.getsource(S.add_engine_tag)
        assert "role_tag_domain" in src, "settings 添加标签必须复用 role_tag_domain()"
        assert '"vod"' not in src, "不应再出现硬编码值域字面量"
        domain = [r for r in role_tag_domain() if r != "all"]
        assert set(domain) == {"chat", "coding", "issue", "qa", "sre", "vod"}, domain


class TestDisabledPredicateConsistency:
    """M4：统计口径与守卫口径必须是同一个「角色值域」谓词。"""

    def test_inventory_and_guard_share_domain(self):
        from src.domains.runtime.public import role_caps_inventory, removing_leaves_no_role, role_tag_domain
        import inspect
        from src.domains.runtime import public as P
        dom = set(role_tag_domain())
        assert dom == {"all", "chat", "coding", "issue", "qa", "sre", "vod"}, dom
        # 两者都用域内判定：库存统计 SQL 含域内 tag 集合，守卫函数按同一常量判定
        inv_src = inspect.getsource(P.role_caps_inventory)
        assert "role_tag_domain()" in inv_src, "统计必须走角色值域"
        assert "NOT LIKE" not in inv_src, "不应再用 NOT LIKE 前缀通配（索引失效且口径不一致）"
        assert "role_tag_domain()" in inspect.getsource(removing_leaves_no_role) or \
               "KNOWN_AGENT_ROLES" in inspect.getsource(removing_leaves_no_role)

