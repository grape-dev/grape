"""#1208 selector 云引擎标签派生守卫（2026-11-11 用户裁定）。

裁定：selector 标签**精确匹配**，engine: 维度必须按 **engine_type** 派生——
云沙箱引擎 name 是 i5 四段全名（hermes:hermes-git-acp:s2:4），按 name 派生
产出 engine:<四段名>，selector 选 engine:hermes 时云引擎永不命中
（实测 S2 s2:4 引擎 0 命中根因）。engine_type 缺失（本机 name==type）回退 name。
"""
import pytest


class TestCloudEngineTagDerivation:
    def test_cloud_four_segment_name_derives_by_engine_type(self):
        """云四段名 + engine_type=hermes → engine:hermes（绝不出四段名标签）。"""
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        tags = derive_agent_tags("hermes:hermes-git-acp:s2:4", "repo", None,
                                 engine_type="hermes")
        assert "engine:hermes" in tags
        assert not any(t == "engine:hermes:hermes-git-acp:s2:4" for t in tags), \
            "云四段名不得作为 engine: 标签值（selector 精确匹配永不命中）"

    def test_local_engine_falls_back_to_name(self):
        """本机引擎（无 engine_type）：沿用 name 派生，零回归。"""
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        tags = derive_agent_tags("hermes", "global", None)
        assert "engine:hermes" in tags
        assert "scope:global" in tags

    def test_selector_matches_cloud_engine_by_type(self):
        """端到端：selector {'engine:hermes'} 对云引擎标签集命中。"""
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        from src.domains.runtime.engine.engine_select import _selector_match
        tags = derive_agent_tags("opencode:hermes-git-a-ode:s2:4", "repo", None,
                                 engine_type="opencode")
        assert _selector_match({"tags": ["engine:opencode"]}, set(tags))
        assert not _selector_match({"tags": ["engine:hermes"]}, set(tags)), \
            "engine_type=opencode 的云引擎不得命中 engine:hermes"

    def test_reachability_query_hits_cloud_engine(self):
        """可达性 SQL 语义：云引擎按 engine_type 标签行参与候选（模拟 _check_pool_selector_reachable 的标签圈选）。"""
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        from src.domains.runtime.engine.engine_select import _selector_match
        # S2 形态: 四段名 + engine_type=hermes + scope repo
        tags = set(derive_agent_tags("hermes:hermes-git-acp:s2:4", "repo", None,
                                     engine_type="hermes"))
        # selector 同时约束 engine + scope 两维度（组间 AND）
        sel = {"tags": ["engine:hermes", "scope:repo"]}
        assert _selector_match(sel, tags)
        # scope 不匹配（user 池选 repo 引擎）→ 不命中
        assert not _selector_match({"tags": ["engine:hermes", "scope:user"]}, tags)
