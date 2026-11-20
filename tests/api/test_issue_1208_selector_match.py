"""Issue #1208 原子契约测试：selector 匹配语义（D14）+ pool_selector 列迁移。

覆盖（Issue 正文 §3 验收 7 / §4 实施顺序 1-2）：
- _selector_match：组内 OR / 组间 AND / 空 selector / 单维度单值 / 多维度混合 /
  未知前缀分组推导 / 大小写归一 / 非法输入容错
- 202611050001：agent_tasks.pool_selector 列幂等加列（TEXT NULL 双后端，#627 教训：
  MySQL TEXT 不带 DEFAULT）
"""
import sqlite3

import pytest

from src.domains.runtime.engine.engine_select import _selector_match


# ===== D14 匹配语义（组内 OR + 组间 AND，无 op 字段）=====

class TestSelectorMatch:
    def test_empty_selector_always_true(self):
        # 空 selector 不约束：任何引擎标签集都命中
        assert _selector_match(None, set()) is True
        assert _selector_match({}, {"engine:opencode"}) is True
        assert _selector_match({"tags": []}, set()) is True

    def test_single_dimension_single_value_hit(self):
        sel = {"tags": ["scope:repo"]}
        assert _selector_match(sel, {"scope:repo", "engine:opencode"}) is True

    def test_single_dimension_single_value_miss(self):
        sel = {"tags": ["scope:repo"]}
        assert _selector_match(sel, {"scope:global"}) is False

    def test_intra_group_or(self):
        # 组内 OR：同维度勾多值 = 任一命中
        sel = {"tags": ["scope:repo", "scope:global"]}
        assert _selector_match(sel, {"scope:global"}) is True
        assert _selector_match(sel, {"scope:repo"}) is True
        assert _selector_match(sel, {"scope:user"}) is False

    def test_inter_group_and(self):
        # 组间 AND：跨维度 = 定语叠加，任一组不满足即不命中
        sel = {"tags": ["scope:repo", "scene:default"]}
        assert _selector_match(sel, {"scope:repo", "scene:default", "engine:hermes"}) is True
        assert _selector_match(sel, {"scope:repo", "scene:cloud-native"}) is False
        assert _selector_match(sel, {"scope:user", "scene:default"}) is False

    def test_ungrouped_dimension_not_constrained(self):
        # 不勾的组 = 不约束（引擎带不带该维度标签都通过）
        sel = {"tags": ["engine:opencode"]}
        assert _selector_match(sel, {"engine:opencode"}) is True
        assert _selector_match(sel, {"engine:opencode", "scope:user", "scene:iac"}) is True
        assert _selector_match(sel, set()) is False

    def test_unknown_prefix_groups_normally(self):
        # D14：无 op 字段，未知前缀（如 label:）按分组推导自然参与匹配
        # （L1 保存校验负责拒绝未上线维度，匹配层不做值域判断）
        sel = {"tags": ["label:gpu"]}
        assert _selector_match(sel, {"label:gpu"}) is True
        assert _selector_match(sel, {"scope:repo"}) is False

    def test_case_normalized(self):
        # 标签统一小写归一（引擎名/值域均小写）
        sel = {"tags": ["scope:repo"]}
        assert _selector_match(sel, {"Scope:Repo"}) is True

    def test_malformed_tags_tolerated(self):
        # 无冒号的标签（畸形）：归入整串组，仍按组语义匹配，不抛异常
        sel = {"tags": ["all"]}
        assert _selector_match(sel, {"all"}) is True
        assert _selector_match(sel, {"scope:repo"}) is False

    def test_selector_tags_not_list_tolerated(self):
        # tags 非列表（脏数据）按空 selector 处理（不约束），不抛异常
        assert _selector_match({"tags": "scope:repo"}, {"scope:repo"}) is True


# ===== 202611050001 pool_selector 幂等加列迁移 =====

def _mk_conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "mig.db"))
    c.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY,
        pool_engine_ids TEXT DEFAULT '',
        pool_engine_uids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued'
    );
    """)
    return c


class TestPoolSelectorMigration:
    def test_column_added_sqlite(self, tmp_path):
        from src.api.db_setup import _migration_1208_add_pool_selector
        c = _mk_conn(tmp_path)
        _migration_1208_add_pool_selector(conn=c)
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_tasks)").fetchall()]
        assert "pool_selector" in cols

    def test_idempotent_rerun(self, tmp_path):
        from src.api.db_setup import _migration_1208_add_pool_selector
        c = _mk_conn(tmp_path)
        _migration_1208_add_pool_selector(conn=c)
        _migration_1208_add_pool_selector(conn=c)  # 两跑不报错
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_tasks)").fetchall()]
        assert cols.count("pool_selector") == 1

    def test_nullable_no_default(self, tmp_path):
        # #627 教训：MySQL TEXT 不能带 DEFAULT；SQLite 侧同样保持 NULL 默认
        from src.api.db_setup import _migration_1208_add_pool_selector
        c = _mk_conn(tmp_path)
        _migration_1208_add_pool_selector(conn=c)
        col = [r for r in c.execute("PRAGMA table_info(agent_tasks)").fetchall() if r[1] == "pool_selector"][0]
        notnull, dflt = col[3], col[4]
        assert not notnull  # 可空
        assert dflt is None  # 无字面 DEFAULT

    def test_missing_table_tolerated(self, tmp_path):
        # 任务表尚未建（异常库形态）→ 不抛错（迁移不阻断启动，README 口径降级）
        from src.api.db_setup import _migration_1208_add_pool_selector
        c = sqlite3.connect(str(tmp_path / "empty.db"))
        _migration_1208_add_pool_selector(conn=c)  # 不抛
