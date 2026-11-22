"""Issue #3796 用例：scene_sync 分类 slug 未命中按同步轮聚合 + category_id 路径过别名。

锁住的契约（Spec：Issue #3796「[issue agent] 评级 ready」评论）：
- 未命中循环内降 debug、进轮级 sink，轮末 ≤1 条聚合 WARNING（slug→次数 + 样例场景）；
- 新告警文案不得含「not found in category_dict」字面量（LogQL 验收计数须归零）；
- category_id（scene_category/_cat_cache）路径与 category_dict 路径同口径先直查后别名；
- 未命中语义保持「保留库内旧值」：cat_l1_id/cat_l2_id 缺席而非写 0 覆盖；
- 旧 slug 经别名落收敛后 1xxx 段（it-rd-*=1001/other-misc=1010/ops-content-goods=1002）。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.runtime.storage import scene_sync
from src.kernel.category import category_seed_dict_map

SS_LOGGER = "src.domains.runtime.storage.scene_sync"

_NODES = [
    {"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "issue", "handoff": "a2a"},
    {"id": "issue", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "done",
     "handoff": "complete"},
]


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_map() -> dict:
    return category_seed_dict_map()


# ===== _record_unresolved：slug → {count, scenes 样例} =====

def test_record_unresolved_counts_and_caps_samples():
    sink: dict = {}
    for i in range(5):
        scene_sync._record_unresolved(sink, "skill-dev", f"scene-{i}")
    assert sink["skill-dev"]["count"] == 5
    assert sink["skill-dev"]["scenes"] == ["scene-0", "scene-1", "scene-2"]  # 样例 ≤3


def test_record_unresolved_dedups_sample_names_and_skips_empty():
    sink: dict = {}
    scene_sync._record_unresolved(sink, "iac", "")
    scene_sync._record_unresolved(sink, "iac", "iac-scene")
    scene_sync._record_unresolved(sink, "iac", "iac-scene")
    assert sink["iac"]["count"] == 3
    assert sink["iac"]["scenes"] == ["iac-scene"]
    scene_sync._record_unresolved(sink, "other", "x")
    assert set(sink) == {"iac", "other"}


# ===== _resolve_scene_category：别名落 1xxx / sink 聚合 / 缺省仍 WARNING =====

def test_resolve_scene_category_alias_lands_on_1xxx():
    with patch.dict(scene_sync._category_dict_cache, _seed_map(), clear=True):
        l1, l2 = scene_sync._resolve_scene_category({"category": "skill-dev"})
        assert l1 == 1001  # it-rd
        assert l2 == 100109  # it-rd-grape-platform（1001*100+9）
        l1, l2 = scene_sync._resolve_scene_category({"category": "general"})
        assert (l1, l2) == (1010, 101002)  # other / other-misc


def test_resolve_scene_category_miss_goes_to_sink_without_warning(caplog):
    with patch.dict(scene_sync._category_dict_cache, _seed_map(), clear=True):
        with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
            sink: dict = {}
            assert scene_sync._resolve_scene_category(
                {"category": "mystery-slug"}, sink, "mystery-scene") == (0, 0)
    assert sink == {"mystery-slug": {"count": 1, "scenes": ["mystery-scene"]}}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_resolve_scene_category_default_path_still_warns(caplog):
    with patch.dict(scene_sync._category_dict_cache, _seed_map(), clear=True):
        with caplog.at_level(logging.WARNING, logger=SS_LOGGER):
            assert scene_sync._resolve_scene_category(
                {"category": "mystery-slug"}) == (0, 0)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "not found in category_dict" in warns[0].getMessage()


# ===== sync_official_scenes 全流程：轮聚合 ≤1 条 WARNING + 别名落库 + 不写 0 覆盖 =====

def _make_zip_bytes(top_dir: str, scenes: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for scene_name, category in scenes.items():
            data = {"name": scene_name, "version": "1.0.0", "category": category,
                    "nodes": _NODES}
            zf.writestr(f"{top_dir}/scenes/{scene_name}/scene.json",
                        json.dumps(data, ensure_ascii=False))
    return buf.getvalue()


def _run_sync_round(zip_bytes: str, cat_cache: dict, captured: list) -> None:
    """按 tests/mgmt/test_scene_sync.py 先例 mock 全流程：下载→解析→落库（捕获 fields）。"""
    async def _mock_download_zip(client, token):
        tmp = tempfile.mkdtemp(prefix="test_sync_3796_")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)
        return os.path.join(tmp, "grape-skills-main")

    def _capture_persist(name, fields, stats):
        captured.append((name, dict(fields)))
        stats["updated"] += 1
        return len(captured), "updated"

    async def _run():
        with patch("src.domains.runtime.storage.scene_sync._get_sync_token",
                   return_value="fake_token"):
            with patch("src.domains.runtime.storage.scene_sync._download_repo_zip",
                       new=_mock_download_zip):
                with patch("src.domains.runtime.storage.scene_sync._refresh_cat_cache"):
                    with patch.dict(scene_sync._cat_cache, cat_cache, clear=True):
                        with patch.dict(scene_sync._category_dict_cache,
                                        _seed_map(), clear=True):
                            with patch("src.domains.runtime.storage.scene_sync._check_skills_exist",
                                       return_value=[]):
                                with patch("src.domains.runtime.storage.scene_sync._persist_scene_row",
                                           side_effect=_capture_persist):
                                    with patch("src.domains.runtime.storage.scene_sync._apply_scene_icon"):
                                        with patch("src.domains.runtime.storage.scene_sync._rebuild_scene_skill_ref_by_name"):
                                            with patch("src.domains.runtime.storage.scene_sync._init_featured_scenes_if_empty"):
                                                with patch("src.domains.runtime.storage.scene_sync._detect_stale_official_scenes",
                                                           return_value=0):
                                                    await scene_sync.sync_official_scenes()
    _run_async(_run())


def test_sync_round_aggregates_misses_into_single_warning(caplog):
    """同 slug 多场景 + 未知 slug：整轮只发 1 条聚合 WARNING（带 slug 计数与样例）。"""
    zip_bytes = _make_zip_bytes("grape-skills-main", {
        "legacy-a": "skill-dev",
        "legacy-b": "skill-dev",
        "mystery-c": "mystery-slug",
    })
    captured: list = []
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        _run_sync_round(zip_bytes, {"other-misc": 7}, captured)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, f"expect exactly 1 aggregated WARNING, got {[w.getMessage() for w in warns]}"
    msg = warns[0].getMessage()
    # 新文案不得再命中 LogQL 验收字符串
    assert "not found in category_dict" not in msg
    assert "unresolved category slugs this round" in msg
    assert "'skill-dev'×2" in msg  # 别名已解析 category_dict 路径，仅 scene_category 侧 2 次未命中
    assert "'mystery-slug'×2" in msg  # 未知 slug：category_dict + scene_category 两条路径都未命中
    assert "legacy-a" in msg  # 样例场景名
    assert "not found in scene_category" not in msg


def test_sync_round_legacy_slug_lands_on_converged_ids_and_keeps_old_on_miss(caplog):
    """旧 slug 经别名落 1xxx；未知 slug 保持「保留库内旧值」（cat_l1/l2 缺席不写 0）。"""
    zip_bytes = _make_zip_bytes("grape-skills-main", {
        "legacy-skill": "skill-dev",
        "general-scene": "general",
        "mystery-c": "mystery-slug",
    })
    captured: list = []
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        _run_sync_round(zip_bytes, {"other-misc": 7}, captured)

    fields = {name: f for name, f in captured}
    # skill-dev → it-rd-grape-platform（1001/100109）；scene_category 侧无该行 → category_id 0
    assert fields["legacy-skill"]["cat_l1_id"] == 1001
    assert fields["legacy-skill"]["cat_l2_id"] == 100109
    assert fields["legacy-skill"]["category_id"] == 0
    # general → other-misc：category_id 路径先直查未命中、过别名命中 7；字典路径落 1010/101002
    assert fields["general-scene"]["category_id"] == 7
    assert fields["general-scene"]["cat_l1_id"] == 1010
    assert fields["general-scene"]["cat_l2_id"] == 101002
    # 未知 slug：不写 0 覆盖既有分类（cat_l1_id/cat_l2_id 不进 UPDATE 字段集）
    assert "cat_l1_id" not in fields["mystery-c"]
    assert "cat_l2_id" not in fields["mystery-c"]
    assert fields["mystery-c"]["category_id"] == 0


def test_sync_round_all_resolved_emits_no_warning(caplog):
    """全部 slug（含别名）命中时轮末不产生任何 WARNING（告警归零口径）。"""
    zip_bytes = _make_zip_bytes("grape-skills-main", {"legacy-a": "skill-dev"})
    captured: list = []
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        _run_sync_round(zip_bytes,
                        {"it-rd-grape-platform": 5},
                        captured)
    assert captured[0][1]["cat_l1_id"] == 1001
    assert captured[0][1]["category_id"] == 5
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_sync_round_direct_cat_cache_hit_wins_over_alias(caplog):
    """scene_category 直查命中时保持原行为（别名只兜底，不抢占既有分类 id）。"""
    zip_bytes = _make_zip_bytes("grape-skills-main", {"legacy-a": "general"})
    captured: list = []
    with caplog.at_level(logging.DEBUG, logger=SS_LOGGER):
        _run_sync_round(zip_bytes, {"general": 3, "other-misc": 7}, captured)
    assert captured[0][1]["category_id"] == 3
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
