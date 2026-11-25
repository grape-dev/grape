"""Issue #3798 用例：skill_sync 分类字典降级口径 + 未命中按轮聚合 + content-generation 补种。

锁住的契约（Spec：Issue #3798「[issue agent] 📋 Spec Document」评论）：

- 字典不可用（读失败 / 读到空字典）：**每轮 1 条 ERROR** + `record_startup_degraded`
  打点（原实现整段吞成 DEBUG），`category_id=0` 降级语义不变、同步不中断；
- N 个技能命中同一缺失 slug：本轮**恰好 1 条** WARNING（含次数 + ≤3 样例），
  逐技能明细只落 DEBUG——对齐 #3796 在 `scene_sync` 落地的口径；
- 聚合文案**不得含**旧字面量 `has no matching skill_category slug`（LogQL 计数须归零）；
- `content-generation` 进种子后，该 `sub_scene` 的官方技能 `category_id` 非 0。
"""
from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import pytest

from src.domains.mgmt.migrations.skill_category_content_generation_seed import (
    SEED_ICON,
    SEED_NAME,
    SEED_ORDER_NUM,
    SEED_SLUG,
    seed_content_generation_category,
    verify_content_generation_category,
)
from src.domains.runtime.storage import skill_sync
from src.kernel.session_manager import db, db_lock

SS_LOGGER = "src.domains.runtime.storage.skill_sync"
#: 验收用字面量：聚合文案不得命中它（Loki 同名告警计数归零）
LEGACY_LITERAL = "has no matching skill_category slug"


def _warns(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def _errors(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR]


# ===== _record_unresolved：slug → {count, skills 样例 ≤3} =====

def test_record_unresolved_counts_and_caps_samples():
    sink: dict = {}
    for i in range(5):
        skill_sync._record_unresolved(sink, "content-generation", f"skill-{i}")
    assert sink["content-generation"]["count"] == 5
    # 样例上限 3（Spec：含次数与 ≤3 样例）
    assert sink["content-generation"]["skills"] == ["skill-0", "skill-1", "skill-2"]


def test_record_unresolved_dedups_samples_and_skips_blank_name():
    sink: dict = {}
    skill_sync._record_unresolved(sink, "no-such-slug", "")
    skill_sync._record_unresolved(sink, "no-such-slug", "a")
    skill_sync._record_unresolved(sink, "no-such-slug", "a")
    assert sink["no-such-slug"]["count"] == 3
    assert sink["no-such-slug"]["skills"] == ["a"]


# ===== _log_unresolved_misses：轮末 ≤1 条聚合 WARNING =====

def test_log_unresolved_misses_emits_single_aggregated_warning(caplog):
    sink: dict = {}
    for name in ("auto-review-pr", "nine-grid-storyboard", "blog-vod", "extra-one"):
        skill_sync._record_unresolved(sink, "content-generation", name)
    skill_sync._record_unresolved(sink, "mystery-slug", "mystery-skill")
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        skill_sync._log_unresolved_misses(sink)
    warns = _warns(caplog)
    assert len(warns) == 1, f"expect exactly 1 aggregated WARNING, got {[w.getMessage() for w in warns]}"
    msg = warns[0].getMessage()
    assert "'content-generation'×4 skills" in msg
    assert "'mystery-slug'×1 skills" in msg
    assert "auto-review-pr" in msg          # 样例技能名
    assert "extra-one" not in msg           # 样例上限 3
    assert LEGACY_LITERAL not in msg        # LogQL 计数归零口径


def test_log_unresolved_misses_silent_when_all_resolved(caplog):
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        skill_sync._log_unresolved_misses({})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===== _load_skill_category_map：字典不可用 → 每轮 1 条 ERROR + 打点 =====

def test_load_category_map_read_failure_logs_one_error_and_metric(caplog):
    with patch("src.domains.mgmt.public.skill_category_active_map",
               side_effect=RuntimeError("no such table: skill_category")):
        with patch.object(skill_sync, "record_startup_degraded") as degraded:
            with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
                assert skill_sync._load_skill_category_map() == {}
    errors = _errors(caplog)
    assert len(errors) == 1, f"expect exactly 1 ERROR, got {[e.getMessage() for e in errors]}"
    assert "skill_category dictionary read failed" in errors[0].getMessage()
    assert "no such table: skill_category" in errors[0].getMessage()
    degraded.assert_called_once_with("skill_category", "read_failed")
    # 不再是静默 DEBUG
    assert not [r for r in caplog.records
                if r.levelno < logging.ERROR and "skill_category dictionary" in r.getMessage()]


def test_load_category_map_empty_logs_one_error_and_metric(caplog):
    with patch("src.domains.mgmt.public.skill_category_active_map", return_value={}):
        with patch.object(skill_sync, "record_startup_degraded") as degraded:
            with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
                assert skill_sync._load_skill_category_map() == {}
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "dictionary is empty" in errors[0].getMessage()
    degraded.assert_called_once_with("skill_category", "empty")


def test_load_category_map_returns_active_map_without_noise(caplog):
    with patch("src.domains.mgmt.public.skill_category_active_map",
               return_value={"content-generation": 16}):
        with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
            assert skill_sync._load_skill_category_map() == {"content-generation": 16}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ===== _sync_skills_to_db 全流程：未命中聚合 + 字典不可用不中断 =====

_ROUND_TAG = uuid.uuid4().hex[:8]


def _round_data(sub_scenes: dict[str, str]) -> dict:
    """构造本轮同步数据（技能名带唯一后缀，用例结束按前缀清理）。"""
    return {f"issue3798-{_ROUND_TAG}-{i}": {"name": f"issue3798-{_ROUND_TAG}-{i}",
                                           "description": "d", "agent_type": "coding",
                                           "path": f"issue3798-{_ROUND_TAG}-{i}",
                                           "sub_scene": slug}
            for i, slug in enumerate(sub_scenes)}


@pytest.fixture()
def cleanup_skills():
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"issue3798-{_ROUND_TAG}-%",))
        db.commit()


def test_sync_round_aggregates_misses_into_single_warning(caplog, cleanup_skills):
    """3 个技能同一缺失 slug + 1 个另一缺失 slug：整轮只发 1 条聚合 WARNING。"""
    data = _round_data(["no-such-slug", "no-such-slug", "no-such-slug", "other-missing"])
    with patch.object(skill_sync, "_load_skill_category_map",
                      return_value={"content-generation": 16}):
        with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
            count = skill_sync._sync_skills_to_db(data)
    assert count == 4
    warns = _warns(caplog)
    assert len(warns) == 1, f"expect exactly 1 aggregated WARNING, got {[w.getMessage() for w in warns]}"
    msg = warns[0].getMessage()
    assert "'no-such-slug'×3 skills" in msg
    assert "'other-missing'×1 skills" in msg
    assert LEGACY_LITERAL not in msg
    # 逐技能明细保留在 DEBUG（不再 WARNING）
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG
              and "unresolved in skill_category" in r.getMessage()]
    assert len(debugs) == 4


def test_sync_round_unresolved_skills_keep_category_id_zero(caplog, cleanup_skills):
    """未命中语义不变：`category_id` 落 0（不编造分类，也不中断同步）。"""
    data = {f"issue3798-{_ROUND_TAG}-zero": {
        "name": f"issue3798-{_ROUND_TAG}-zero", "description": "d",
        "agent_type": "coding", "path": f"issue3798-{_ROUND_TAG}-zero",
        "sub_scene": "no-such-slug"}}
    with patch.object(skill_sync, "_load_skill_category_map",
                      return_value={"content-generation": 16}):
        with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
            assert skill_sync._sync_skills_to_db(data) == 1
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT category_id, sub_scene FROM skill "
                        "WHERE skill_name LIKE ?", (f"issue3798-{_ROUND_TAG}-%",)).fetchone()
    assert row[0] == 0
    assert row[1] == "no-such-slug"


def test_sync_round_all_resolved_emits_no_warning(caplog, cleanup_skills):
    """全部命中时轮末不产生任何 WARNING（告警归零口径）。"""
    data = _round_data(["content-generation", "content-generation"])
    with patch.object(skill_sync, "_load_skill_category_map",
                      return_value={"content-generation": 16}):
        with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
            assert skill_sync._sync_skills_to_db(data) == 2
    assert not _warns(caplog)


def test_sync_round_dictionary_unavailable_errors_once_and_aggregates(
        caplog, cleanup_skills):
    """真实降级路径（缺表/读失败）：1 条 ERROR + 打点 + 1 条聚合 WARNING，同步不中断。"""
    data = _round_data(["content-generation", "content-generation"])
    with patch("src.domains.mgmt.public.skill_category_active_map",
               side_effect=RuntimeError("no such table: skill_category")):
        with patch.object(skill_sync, "record_startup_degraded") as degraded:
            with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
                assert skill_sync._sync_skills_to_db(data) == 2
    degraded.assert_called_once_with("skill_category", "read_failed")
    assert len(_errors(caplog)) == 1
    warns = _warns(caplog)
    assert len(warns) == 1
    assert "'content-generation'×2 skills" in warns[0].getMessage()
    assert LEGACY_LITERAL not in warns[0].getMessage()


# ===== content-generation 种子：幂等补种 + 命中后 category_id 非 0 =====

def test_seed_content_generation_category_is_idempotent():
    """迁移可重复执行：行只在位一行，且启用（重复执行不改行数）。"""
    seed_content_generation_category()
    seed_content_generation_category()
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT name, icon, order_num, is_active FROM skill_category WHERE slug=?",
            (SEED_SLUG,)).fetchall()
    assert len(rows) == 1, f"种子行应唯一（按 slug），实际 {len(rows)} 行"
    assert rows[0] == (SEED_NAME, SEED_ICON, SEED_ORDER_NUM, 1)


def test_seed_content_generation_visible_in_active_map():
    """同步侧只读启用行：补种后 `skill_category_active_map()` 必须含该 slug。"""
    from src.domains.mgmt.public import skill_category_active_map

    seed_content_generation_category()
    assert skill_category_active_map().get(SEED_SLUG, 0) > 0


def test_seed_post_check_raises_when_row_missing():
    """post_check 是盖章闸门：行缺失必须 raise（不允许静默盖章）。"""
    class _NoRowConn:
        def cursor(self):
            class _C:
                def execute(self, *_a, **_kw):
                    class _R:
                        def fetchone(self):
                            return None
                    return _R()
            return _C()

    with pytest.raises(RuntimeError, match="content-generation"):
        verify_content_generation_category(conn=_NoRowConn())


def test_seeded_slug_resolves_category_id(cleanup_skills):
    """种子命中后该 sub_scene 的技能 `category_id` 非 0（Spec 验收项）。"""
    seed_content_generation_category()
    with db_lock:
        c = db.cursor()
        category_id = int(c.execute("SELECT id FROM skill_category WHERE slug=?",
                                    (SEED_SLUG,)).fetchone()[0])
    assert category_id > 0

    data = _round_data(["content-generation"])
    assert skill_sync._sync_skills_to_db(data) == 1
    with db_lock:
        c = db.cursor()
        stored = int(c.execute("SELECT category_id FROM skill WHERE skill_name LIKE ?",
                               (f"issue3798-{_ROUND_TAG}-%",)).fetchone()[0])
    assert stored == category_id