"""技能分类切换迁移（202611230001/2）的数据契约测试（两轴审查 Standards#6）。

单一真源 = 各属主域 migrations 模块；本测试直接 import 断言（不再从
db_setup.py 正则提取，接线层不承载契约）。
"""
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

from src.domains.mgmt.migrations.skill_category_cutover_dict import (
    MIGRATION as MGMT_MIGRATION,
)
from src.domains.runtime.migrations.skill_category_cutover import (
    EXPECTED_DISTRIBUTION,
    OFFICIAL_SKILL_L2_MAP,
    MIGRATION as RT_MIGRATION,
)
from src.kernel.category import (
    CATEGORY_DICT_SEED as _CATEGORY_DICT_SEED,
    category_dict_seed_id,
)


# ── 契约一：迁移登记口径（kind=backfill + post_check + 属主域 SQL 落位） ──

def test_migration_registration_contract():
    """两条迁移：kind=backfill、带 post_check、domains 与属主一致。"""
    assert MGMT_MIGRATION["version"] == "202611230001"
    assert MGMT_MIGRATION["kind"] == "backfill"
    assert MGMT_MIGRATION["domains"] == ("mgmt",)
    assert callable(MGMT_MIGRATION["post_check"])
    assert RT_MIGRATION["version"] == "202611230002"
    assert RT_MIGRATION["kind"] == "backfill"
    assert RT_MIGRATION["domains"] == ("runtime",)
    assert callable(RT_MIGRATION["post_check"])


def test_migration_sql_lives_in_owner_domain():
    """P0-2 跨域守卫：迁移 SQL 住属主域文件，db_setup.py 只留接线（QA A8/A11）。"""
    db_setup = (REPO / "src" / "api" / "db_setup.py").read_text(encoding="utf-8")
    i = db_setup.find("技能分类切换（统一字典 l1/l2 收口）")
    assert i > 0, "db_setup.py 缺 cutover 接线注释块"
    wiring = db_setup[i:i + 900]
    assert "from src.domains.mgmt.migrations.skill_category_cutover_dict import" in wiring
    assert "from src.domains.runtime.migrations.skill_category_cutover import" in wiring
    # 接线层不允许承载迁移数据/SQL（跨域直写由 test_foreign_table_refs 另守）
    assert "_OFFICIAL_SKILL_L2_MAP" not in db_setup


# ── 契约二：映射表（74 条全量、唯一、slug 全可复算） ──

def test_map_entries_unique():
    names = [n for n, _ in OFFICIAL_SKILL_L2_MAP]
    assert len(names) == len(set(names)), "skill_name 重复：唯一性被破坏"


def test_map_count_76():
    # 74 + QA B2 补的 cce-terrapilot / egress-test（PR #1955 原子回归拦截点）
    assert len(OFFICIAL_SKILL_L2_MAP) == 76


def test_all_l2_slugs_resolvable_in_seed():
    """运行期 _resolve_ids 同口径前置到测试期：slug 缺失即红。"""
    id_by_slug = {}
    for idx, row in enumerate(_CATEGORY_DICT_SEED):
        id_by_slug[row[3]] = category_dict_seed_id(_CATEGORY_DICT_SEED, idx)
    missing = sorted({slug for _, slug in OFFICIAL_SKILL_L2_MAP} - set(id_by_slug))
    assert not missing, f"种子字典缺二级 slug: {missing}"


# ── 契约三：拍板口径钉死（2026-11-22 用户逐条确认） ──

def test_pinned_ids_and_decisions():
    id_by_slug = {}
    l1_of_l2 = {}
    for idx, row in enumerate(_CATEGORY_DICT_SEED):
        id_by_slug[row[3]] = category_dict_seed_id(_CATEGORY_DICT_SEED, idx)
        if int(row[1]) == 2:
            l1_of_l2[row[3]] = id_by_slug.get(row[0], 0)
    # grape平台应用 = 100109（mgmt/migrations 模块的钉子）
    from src.domains.mgmt.migrations.skill_category_cutover_dict import NEW_L2_SLUG
    assert id_by_slug[NEW_L2_SLUG] == 100109
    assert l1_of_l2[NEW_L2_SLUG] == 1001
    # 组织仓库 AI 分析 → 测试与质量（用户拍板）
    assert dict(OFFICIAL_SKILL_L2_MAP)["org-repo-analyzer"] == "it-rd-test-quality"
    assert id_by_slug["it-rd-test-quality"] == 100104


def test_distribution_pinned():
    c = Counter(slug for _, slug in OFFICIAL_SKILL_L2_MAP)
    assert dict(c) == EXPECTED_DISTRIBUTION, f"归属分布漂移: {dict(c)}"


# ── 契约四：失败语义（空表先探测后写；未命中 raise 不盖章） ──

def test_fn_raises_when_no_official_skills():
    """空库时序防护：官方技能数为 0 时，任何写之前 raise（Spec#3 探测前置）。"""
    import sqlite3

    import src.domains.runtime.migrations.skill_category_cutover as mod

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE skill (skill_name TEXT, scope TEXT, cat_l1_id INTEGER DEFAULT 0,"
        " cat_l2_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0)")
    with pytest.raises(RuntimeError, match="官方技能尚未入库"):
        mod.cutover_official_skills(conn=conn)


def test_fn_raises_on_partial_hit():
    """名单漂移：未命中 skill_name 必须 raise（不允许部分挂靠却盖章，Spec#2）。"""
    import sqlite3

    import src.domains.runtime.migrations.skill_category_cutover as mod

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE skill (skill_name TEXT, scope TEXT, cat_l1_id INTEGER DEFAULT 0,"
        " cat_l2_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO skill (skill_name, scope) VALUES ('vod', 'official')")
    # 只插入 1 个映射里的技能 → 其余 73 个未命中
    with pytest.raises(RuntimeError, match="挂靠未全命中"):
        mod.cutover_official_skills(conn=conn)
