"""Issue #3796 kernel 用例：legacy category slug 别名过渡 + 校验函数。

锁住的契约（Spec：Issue #3796「[issue agent] 评级 ready」评论）：
- LEGACY_CATEGORY_ALIASES 六条映射（skill-dev/iac/general/content/ai/swarm-dev → 收敛后 slug）；
- lookup_slug / resolve_category_slugs 先直查后别名，未命中仍 (0,0)（保留库内旧值语义）；
- 别名生效每进程每 slug 仅记 1 条 INFO（非 WARNING，防同步每轮刷屏）；
- validate_category_slugs 拦截「直查与别名均未命中」的 slug，供启动/CI 校验；
- scenes/*/scene.json 扫描 + 缺目录降级跳过（部署根 scenes/ 由 grape-skills 下发）。
"""
from __future__ import annotations

import json
import logging
import os

import pytest

import src.kernel.category as category_mod
from src.kernel.category import (
    CATEGORY_DICT_SEED,
    LEGACY_CATEGORY_ALIASES,
    canonical_category_slug,
    category_dict_seed_id,
    category_seed_dict_map,
    iter_scenes_category_slugs,
    lookup_slug,
    resolve_category_slugs,
    scene_category_slug_values,
    validate_category_slugs,
    validate_local_scenes_categories,
)

LOGGER_NAME = "src.kernel.category"


@pytest.fixture(autouse=True)
def _reset_alias_state():
    """每个用例前清空每进程去重集合（跨模块用例已触发别名时也不串味），结束后恢复。"""
    saved = set(category_mod._alias_warned)
    category_mod._alias_warned.clear()
    yield
    category_mod._alias_warned.clear()
    category_mod._alias_warned.update(saved)


def _seed_map() -> dict:
    return category_seed_dict_map()


def _seed_ids() -> dict:
    return {row[3]: category_dict_seed_id(CATEGORY_DICT_SEED, i)
            for i, row in enumerate(CATEGORY_DICT_SEED)}


# ===== 别名表（纯数据，独立可调） =====

def test_alias_table_covers_all_legacy_slugs():
    assert LEGACY_CATEGORY_ALIASES == {
        "skill-dev": "it-rd-grape-platform",
        "iac": "it-rd-infra",
        "general": "other-misc",
        "content": "ops-content-goods",
        "ai": "it-rd-ai-app",
        "swarm-dev": "it-rd-engineering",
    }


def test_alias_targets_all_exist_in_seed():
    """别名目标必须全部在收敛后的种子字典里（防别名表自身漂移）。"""
    slugs = {row[3] for row in CATEGORY_DICT_SEED}
    for _old, new in LEGACY_CATEGORY_ALIASES.items():
        assert new in slugs, f"alias target '{new}' missing in CATEGORY_DICT_SEED"
        assert _old not in slugs, f"legacy slug '{_old}' should have been collapsed out"


# ===== lookup_slug：先直查后别名 =====

def test_lookup_slug_direct_hit_unchanged():
    m = _seed_map()
    assert lookup_slug(m, "it-rd")[0] == 1001
    assert lookup_slug(m, "it-rd") == (1001, 0)


def test_lookup_slug_alias_hit_lands_on_1xxx():
    m = _seed_map()
    ids = _seed_ids()
    # 旧 slug 直查未命中 → 别名命中收敛后二级（cat_l1=1xxx、cat_l2=父级*100+order）
    for old, new in LEGACY_CATEGORY_ALIASES.items():
        l1, l2 = lookup_slug(m, old)
        assert (l1, l2) == (lookup_slug(m, new)), f"alias '{old}' should resolve like '{new}'"
        assert 1001 <= l1 <= 1010, f"{old}: l1 {l1} not in converged 1xxx band"
        assert l2 == ids[new]


def test_lookup_slug_unknown_returns_zero_pair():
    m = _seed_map()
    assert lookup_slug(m, "no-such-slug") == (0, 0)
    assert lookup_slug(m, "") == (0, 0)
    assert lookup_slug({}, "skill-dev") == (0, 0)


def test_lookup_slug_direct_hit_wins_over_alias():
    """管理台自定义同名 slug 时以直查为准（别名只兜底，不抢占）。"""
    m = dict(_seed_map())
    m["general"] = {"id": 9001, "level": 1, "parent_id": 0}
    assert lookup_slug(m, "general") == (9001, 0)


# ===== resolve_category_slugs：别名生效 + on_miss 聚合回调 =====

def test_resolve_legacy_single_category_slugs():
    m = _seed_map()
    ids = _seed_ids()
    assert resolve_category_slugs({"category": "skill-dev"}, m) == (1001, ids["it-rd-grape-platform"])
    assert resolve_category_slugs({"category": "iac"}, m) == (1001, ids["it-rd-infra"])
    assert resolve_category_slugs({"category": "general"}, m) == (1010, ids["other-misc"])
    assert resolve_category_slugs({"category": "content"}, m) == (1002, ids["ops-content-goods"])
    assert resolve_category_slugs({"category": "ai"}, m) == (1001, ids["it-rd-ai-app"])
    assert resolve_category_slugs({"category": "swarm-dev"}, m) == (1001, ids["it-rd-engineering"])


def test_resolve_keeps_miss_semantics_zero_pair():
    """未命中语义不变：返回 (0, 0)，由调用方保留库内旧值（不写 0 覆盖）。"""
    m = _seed_map()
    assert resolve_category_slugs({"category": "no-such-slug"}, m) == (0, 0)


def test_resolve_on_miss_suppresses_warning_and_calls_back(caplog):
    """on_miss 提供时未命中降为 debug + 回调，不再刷 WARNING（Issue #3796 轮聚合入口）。"""
    m = _seed_map()
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        result = resolve_category_slugs({"category": "no-such-slug"}, m, on_miss=seen.append)
    assert result == (0, 0)
    assert seen == ["no-such-slug"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_resolve_default_path_still_warns_once_per_slug(caplog):
    """缺省（on_miss=None）保持原 WARNING 行为——skill_sync 等调用方零改动。"""
    m = _seed_map()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        resolve_category_slugs({"category": "no-such-slug"}, m)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "not found in category_dict" in warns[0].getMessage()


def test_resolve_on_miss_reports_l1_l2_and_category_paths(caplog):
    m = _seed_map()
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        resolve_category_slugs(
            {"category_l1": "bad-l1", "category_l2": "bad-l2", "category": "bad/cat"},
            m, on_miss=seen.append)
    assert sorted(seen) == ["bad-l1", "bad-l2", "bad/cat"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===== canonical_category_slug + 一次性 INFO =====

def test_canonical_slug_maps_legacy_and_passes_unknown_through():
    assert canonical_category_slug("skill-dev") == "it-rd-grape-platform"
    assert canonical_category_slug(" general ") == "other-misc"
    assert canonical_category_slug("it-rd") == "it-rd"
    assert canonical_category_slug("no-such-slug") == "no-such-slug"
    assert canonical_category_slug("") == ""


def test_alias_info_logged_once_per_slug_per_process(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        canonical_category_slug("skill-dev")
        canonical_category_slug("skill-dev")
        lookup_slug(_seed_map(), "iac")
    infos = [r for r in caplog.records
             if r.levelno == logging.INFO and "legacy slug" in r.getMessage()]
    assert len(infos) == 2  # skill-dev 1 条 + iac 1 条（重复调用不重复记）
    assert all("skill-dev" not in r.getMessage() or "it-rd-grape-platform" in r.getMessage()
               for r in infos)
    # 别名生效是 INFO，不是 WARNING（防 LogQL 告警计数回升）
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===== validate_category_slugs（启动/CI 校验） =====

def test_validate_returns_only_unknown_slugs_deduped():
    m = _seed_map()
    unknown = validate_category_slugs(
        ["skill-dev", "iac", "no-such-slug", "no-such-slug", "it-rd", ""], m)
    assert unknown == ["no-such-slug"]


def test_validate_empty_inputs():
    assert validate_category_slugs([], _seed_map()) == []
    assert validate_category_slugs(["skill-dev"], {}) == ["skill-dev"]
    assert validate_category_slugs(None, _seed_map()) == []


# ===== scene_category_slug_values（slug 提取口径） =====

def test_scene_slug_values_extracts_all_fields_and_paths():
    assert scene_category_slug_values(
        {"category_l1": "it-rd", "category_l2": "it-rd-infra", "category": "a/b"}) == \
        ["it-rd", "it-rd-infra", "a", "b"]
    assert scene_category_slug_values({}) == []
    assert scene_category_slug_values({"category": "general"}) == ["general"]


# ===== 种子映射（CI/启动无 DB 口径） =====

def test_seed_dict_map_matches_seed_ids():
    m = _seed_map()
    ids = _seed_ids()
    assert m["it-rd"]["id"] == 1001 and m["it-rd"]["level"] == 1
    assert m["it-rd-grape-platform"] == {"id": ids["it-rd-grape-platform"],
                                         "level": 2, "parent_id": 1001}
    assert m["other-misc"]["parent_id"] == 1010


# ===== scenes/*/scene.json 扫描（缺目录降级） =====

def _write_scene(scenes_dir: str, name: str, category: str | None,
                 l1: str = "", l2: str = "") -> None:
    scene = {"name": name}
    if category is not None:
        scene["category"] = category
    if l1:
        scene["category_l1"] = l1
    if l2:
        scene["category_l2"] = l2
    d = os.path.join(scenes_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "scene.json"), "w", encoding="utf-8") as f:
        json.dump(scene, f, ensure_ascii=False)


def test_iter_scenes_category_slugs(tmp_path):
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "legacy-scene", "skill-dev")
    _write_scene(scenes, "bad-scene", "no-such-slug", l1="it-rd", l2="bad-l2")
    _write_scene(scenes, "no-category", None)
    result = iter_scenes_category_slugs(scenes)
    assert result["legacy-scene"] == ["skill-dev"]
    assert result["bad-scene"] == ["it-rd", "bad-l2", "no-such-slug"]
    assert "no-category" not in result


def test_iter_scenes_missing_dir_degrades_to_empty(tmp_path):
    assert iter_scenes_category_slugs(str(tmp_path / "nope")) == {}
    assert iter_scenes_category_slugs("") == {}


def test_validate_local_scenes_flags_unknown_and_passes_alias(tmp_path):
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "legacy-ok", "iac")
    _write_scene(scenes, "legacy-bad", "skill-dev", l2="no-such-l2")
    unknown = validate_local_scenes_categories(scenes)
    assert unknown == {"legacy-bad": ["no-such-l2"]}


def test_validate_local_scenes_missing_dir_returns_empty(tmp_path):
    assert validate_local_scenes_categories(str(tmp_path / "nope")) == {}


# ===== 别名链单一真源（PR #1982 审查 Nit）：校验与解析共用同一解析函数 =====

def test_alias_target_follows_multi_hop_chain(monkeypatch):
    """多级别名链由同一纯函数解析：校验不会因为「二级别名」误报未知 slug。"""
    from src.kernel.category import alias_target

    monkeypatch.setitem(LEGACY_CATEGORY_ALIASES, "ai-legacy", "ai")
    assert alias_target("ai-legacy") == "it-rd-ai-app"
    assert alias_target("ai") == "it-rd-ai-app"
    assert alias_target("it-rd") == "it-rd"
    assert alias_target("") == ""
    # lookup 走同一链
    assert lookup_slug(category_seed_dict_map(), "ai-legacy") == (1001, 100102)


def test_validate_category_slugs_uses_alias_chain(monkeypatch):
    """校验与 lookup_slug 同口径：二级别名解析到的 slug 不得被判为未知。"""
    monkeypatch.setitem(LEGACY_CATEGORY_ALIASES, "ai-legacy", "ai")
    assert validate_category_slugs(["ai-legacy", "skill-dev"], category_seed_dict_map()) == []
    assert validate_category_slugs(["ai-legacy", "no-such"], category_seed_dict_map()) == ["no-such"]


def test_validate_local_scenes_accepts_explicit_dict_snapshot(tmp_path):
    """启动/CI 可传入导出的字典快照（DB active 口径），缺省回落种子口径。"""
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "scene-a", "it-rd")
    _write_scene(scenes, "scene-b", "general")
    # 快照里没有 other-misc（模拟线上 DB 停用该行）→ general 的别名目标未命中
    snapshot = {k: v for k, v in category_seed_dict_map().items() if k != "other-misc"}
    assert validate_local_scenes_categories(scenes, dict_map=snapshot) == {"scene-b": ["general"]}
    # 缺省仍按种子口径（含 other-misc）→ 通过
    assert validate_local_scenes_categories(scenes) == {}
