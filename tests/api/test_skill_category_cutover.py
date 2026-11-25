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


# ── 契约二：映射表（唯一、slug 全可复算、与生产注册表集合对齐） ──

#: 生产注册表（skill-description.json）实际发布的官方技能 74 条。
#: 独立硬编码快照，与被测映射表不同源——映射表/注册表任一漂移都在单测阶段暴露。
#: 映射表当前 76 条 = 本集合 74 条 + 2 条 dev-only 死条目（见 KNOWN_DEV_ONLY）。
REGISTRY_OFFICIAL_SKILLS = frozenset({
    "_template", "ai-blog-generation", "auto-review-pr", "blog-issue",
    "blog-manager", "blog-publish", "blog-vod", "build-debug",
    "changelog-gen", "chat", "chat-cli", "cloud-deploy",
    "coding-cli", "coding-mattpocock", "common-skill-creator", "common-skill-tester",
    "deploy-cli", "deploy-guard", "devbridge-deploy", "domain-coding",
    "domain-deploy", "domain-issue", "domain-qa", "domain-vod",
    "event-dialogue", "grape-skill-generator", "grape-skill-issue", "grape-skill-uploader",
    "grape-skill-validator", "grape-vod-cli", "huawei-cloud", "huawei-cloud-devbridge-deploy",
    "iac-coding", "iac-issue", "iac-qa", "iac-sre",
    "iac-vod", "im-notify", "impact-analysis", "implementation",
    "incident-triage", "issue", "issue-advance", "issue-cli",
    "issue-mattpocock", "issue-multitask", "narrated-explainer-video", "nine-grid-storyboard",
    "novel-to-manga", "org-repo-analyzer", "os-disk-list", "plugin-creator",
    "plugin-publish", "plugin-tester", "pr", "qa",
    "qa-cli", "qa-mattpocock", "qwen-image-generation", "repo-onboarding",
    "rest-api-skill-creator", "rest-api-skill-tester", "runtime-install", "skill-creator",
    "skill-issue", "skill-pr", "skill-publish", "skill-tester",
    "vod", "vod-advance", "vod-cli", "vod-mattpocock",
    "vod-strict", "wan-video-generation",
})

#: 映射表相对注册表多出的已知 dev-only 死条目（QA B2，PR #1955）：dev 在库、
#: 生产注册表未发布。迁移对它们仅告警跳过；去留待产品决策，本单不删条目。
KNOWN_DEV_ONLY = frozenset({"cce-terrapilot", "egress-test"})


def test_map_entries_unique():
    names = [n for n, _ in OFFICIAL_SKILL_L2_MAP]
    assert len(names) == len(set(names)), "skill_name 重复：唯一性被破坏"


def test_map_aligned_with_registry():
    """集合对齐注册表：库内 official 必须全被覆盖，多出的只能是已知死条目。"""
    names = {n for n, _ in OFFICIAL_SKILL_L2_MAP}
    missing = REGISTRY_OFFICIAL_SKILLS - names
    assert not missing, f"映射表漏配注册表内 official 技能: {sorted(missing)}"
    extra = names - REGISTRY_OFFICIAL_SKILLS
    assert extra == KNOWN_DEV_ONLY, (
        f"映射表相对注册表的漂移超出已知死条目: {sorted(extra)}"
        "（新增/删除条目须同步 REGISTRY_OFFICIAL_SKILLS 与 KNOWN_DEV_ONLY）")


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


# ── 契约四：失败语义（空表先探测后写；双向差集：库多 raise / map 多告警盖章） ──

def _make_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE skill (skill_name TEXT, scope TEXT, cat_l1_id INTEGER DEFAULT 0,"
        " cat_l2_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0)")
    return conn


def test_fn_raises_when_no_official_skills():
    """空库时序防护：官方技能数为 0 时，任何写之前 raise（Spec#3 探测前置）。"""
    import src.domains.runtime.migrations.skill_category_cutover as mod

    conn = _make_conn()
    with pytest.raises(RuntimeError, match="官方技能尚未入库"):
        mod.cutover_official_skills(conn=conn)


def test_fn_raises_when_db_has_unmapped_official():
    """库内 official 多于映射表（真漏配）→ raise 且一字节不写、不盖章。"""
    import src.domains.runtime.migrations.skill_category_cutover as mod

    conn = _make_conn()
    conn.execute("INSERT INTO skill (skill_name, scope) VALUES ('vod', 'official')")
    conn.execute(
        "INSERT INTO skill (skill_name, scope) VALUES ('brand-new-official', 'official')")
    conn.commit()
    with pytest.raises(RuntimeError, match="未被映射表覆盖"):
        mod.cutover_official_skills(conn=conn)
    # raise 先于任何写：已挂靠行不得被更新
    row = conn.execute(
        "SELECT cat_l1_id, cat_l2_id FROM skill WHERE skill_name='vod'").fetchone()
    assert row == (0, 0), f"raise 前不应写入，实际 {row}"


def test_fn_warns_and_stamps_when_map_has_extra(monkeypatch):
    """映射表多于库内（死条目残留）→ 仅 1 条 WARNING、跳过不阻断，正常挂靠盖章。"""
    import src.domains.runtime.migrations.skill_category_cutover as mod
    import src.kernel.config as config_mod

    warnings: list = []
    infos: list = []
    fake_log = type("_L", (), {
        "warning": staticmethod(lambda msg: warnings.append(msg)),
        "info": staticmethod(lambda msg: infos.append(msg)),
    })
    monkeypatch.setattr(config_mod, "log", fake_log)

    conn = _make_conn()
    for name in sorted(REGISTRY_OFFICIAL_SKILLS):
        conn.execute(
            "INSERT INTO skill (skill_name, scope) VALUES (?, 'official')", (name,))
    conn.commit()

    # 不抛异常（死条目 cce-terrapilot / egress-test 未入库也不阻断）
    mod.cutover_official_skills(conn=conn)

    assert len(warnings) == 1, f"应聚合为 1 条 WARNING，实际 {len(warnings)}"
    assert "cce-terrapilot" in warnings[0] and "egress-test" in warnings[0]
    assert len(infos) == 1, f"应仅 1 条 INFO，实际 {len(infos)}"
    assert f"/{len(REGISTRY_OFFICIAL_SKILLS)}" in infos[0]

    # 交集全部挂靠、无漏网
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM skill WHERE scope='official' AND cat_l1_id=0"
    ).fetchone()[0]
    assert unmapped == 0, f"仍有 {unmapped} 条 official 未挂靠"


def test_fn_idempotent_second_run(monkeypatch):
    """二次运行幂等：挂靠值已正确时不重复写、不 raise（保留先读终值口径）。"""
    import src.domains.runtime.migrations.skill_category_cutover as mod
    import src.kernel.config as config_mod

    fake_log = type("_L", (), {
        "warning": staticmethod(lambda msg: None),
        "info": staticmethod(lambda msg: None),
    })
    monkeypatch.setattr(config_mod, "log", fake_log)

    conn = _make_conn()
    for name in sorted(REGISTRY_OFFICIAL_SKILLS):
        conn.execute(
            "INSERT INTO skill (skill_name, scope) VALUES (?, 'official')", (name,))
    conn.commit()

    mod.cutover_official_skills(conn=conn)
    mod.cutover_official_skills(conn=conn)  # 二次运行不得 raise

    stale = conn.execute(
        "SELECT COUNT(*) FROM skill WHERE category_id<>0").fetchone()[0]
    assert stale == 0
