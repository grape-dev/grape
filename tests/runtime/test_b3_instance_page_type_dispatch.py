"""[P0b B6 尾/C08-A2 收编] 实例名→页面类型/图标 单源派生 + 前端 includes 链清零守卫。

背景（2026-11-14 裁定·第三案）：runtimes/dashboard/autopilot 三页各自手抄
`includes('hermes')` 归并链（5 处）属分派知识——与后端 settings._engine_type_of
同一份知识的前端副本。收编方式：后端在实例行上 annotate `page_type`（经
page_type_of_instance 单源派生，quirk 兜底链随函数整体迁移），meta 端点补
page_type/icon 字段；前端三链删除改读字段。
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


class TestPageTypeOfInstance:
    """engine/instances.py page_type_of_instance：settings._engine_type_of 的单源化迁移。

    quirk 兜底链（非前缀历史名 includes 判定，顺序敏感）随函数迁移原样保留——
    链是旧轨判定不是分派知识（B3 点 7 裁定），但只允许存在一份。
    """

    @pytest.mark.parametrize("name,expected", [
        # canonical 前缀/精确
        ("hermes", "hermes"),
        ("hermes-worker-1", "hermes"),
        ("opencode", "opencode"),
        ("hwcloud-1", "aishell"),          # hwcloud 特例=历史页签
        ("aishell-dsh-worker-1", "aishell-dsh"),
        ("aishell-worker-1", "aishell"),
        ("codearts", "codearts"),
        ("codex-atomcode-1", "codex"),  # 顺序敏感：codex 先于 atomcode
        ("atomcode-x", "atomcode"),
        # [行为冻结] 下列两条为基线既有行为（wt-p0-baseline 实测一致），非本轮改动引入：
        # cursor=ghost 引擎不在 prefix_map → canonical_engine 兜底 opencode → 链无 hit → ""
        ("cursor", ""),
        # 云四段名(opencode:hermes-git-a-ode:s2:4)——arch-weekly-11 E-02
        # (2026-11-16) 已裁定对齐首段：canonical 首段=opencode 为权威类型，不落
        # includes 兜底（模板名段可含任意引擎词）。本 cherry-pick 融合保留该守卫。
        ("opencode:hermes-git-a-ode:s2:4", "opencode"),
        # 空名/未知名
        ("", ""),
        ("unknown-engine", ""),
        (None, ""),
    ])
    def test_derivation(self, name, expected):
        from src.domains.runtime.engine.instances import page_type_of_instance
        assert page_type_of_instance(name) == expected

    def test_settings_engine_type_of_delegates(self):
        """settings._engine_type_of 保留（既有测试/调用面不破坏）但改为委托单源函数。"""
        from src.domains.mgmt.routes.settings import _engine_type_of
        from src.domains.runtime.engine.instances import page_type_of_instance
        for n in ("hermes-worker-1", "aishell-dsh-node2", "hwcloud-1", "codex-atomcode-1", ""):
            rt = {"name": n}
            assert _engine_type_of(rt) == page_type_of_instance(n)


class TestEnginesMetaPageTypeIcon:
    """/api/engines/meta 补 page_type + display.icon（watermark 水印图/类型筛选消费）。"""

    def test_meta_fields(self):
        from src.domains.mgmt.routes.engines import engines_meta
        metas = engines_meta()["engines"]
        by_c = {m["canonical"]: m for m in metas}
        # page_type：canonical→页面类型（hwcloud→aishell）
        assert by_c["hwcloud"]["page_type"] == "aishell"
        assert by_c["aishell-dsh"]["page_type"] == "aishell-dsh"
        assert by_c["hermes"]["page_type"] == "hermes"
        # icon：页面引擎图标资产路径（Runtimes 水印等）
        assert by_c["hwcloud"]["display"]["icon"] == "/static/img/engines/AIShell.svg"
        assert by_c["aishell-dsh"]["display"]["icon"] == "/static/img/engines/AIShellDsh.svg"
        assert by_c["hermes"]["display"]["icon"] == "/static/img/engines/Hermes.png"
        assert by_c["opencode"]["display"]["icon"] == "/static/img/engines/opencode.png"


class TestFrontendIncludesGuard:
    """C08-A2 口径：引擎名 includes 归并链清零（实例归并读 page_type 字段）。

    arch-weekly-11 基线：前端为 Vue（src/api/frontend/app）。B6 page_type/meta
    消费语义已移植到四个 Vue 视图（2026-11-17）：类型归并读后端 annotate 的
    page_type，徽标颜色/水印图标读 /api/engines/meta（lib/engineMeta.js 单源
    消费模块）——本守卫扫 VIEWS，includes 引擎名链必须为 0 命中。
    """

    VIEWS = [
        "src/api/frontend/app/src/views/AgentView.vue",
        "src/api/frontend/app/src/views/AutopilotView.vue",
        "src/api/frontend/app/src/views/DashboardView.vue",
        "src/api/frontend/app/src/views/RuntimesView.vue",
    ]
    PAGES = VIEWS
    PAT = re.compile(r"includes\(\s*['\"](?:hermes|opencode|codearts|codex|atomcode|aishell)")

    def test_no_engine_name_includes_chains(self):
        hits = []
        for rel in self.PAGES:
            text = (_ROOT / rel).read_text(encoding="utf-8")
            for m in self.PAT.finditer(text):
                hits.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}:{m.group(0)}")
        assert not hits, f"前端引擎名 includes 链残留 {len(hits)} 处:\n" + "\n".join(hits)

    def test_pages_read_page_type_field(self):
        # 类型归并三视图读后端 annotate 的 page_type；
        # AgentView 的 B6 消费面是 /api/engines/meta（徽标颜色经 lib/engineMeta.js）
        for rel in self.PAGES:
            text = (_ROOT / rel).read_text(encoding="utf-8")
            if "AgentView" in rel:
                assert "engineMeta" in text, f"{rel} 未消费 engines/meta（应经 lib/engineMeta.js 读徽标颜色）"
            else:
                assert "page_type" in text, f"{rel} 未消费 page_type 字段（应读后端 annotate 值）"
