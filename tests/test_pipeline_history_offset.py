"""Issue #542 前端补齐：get_pipeline history_offset 增量分页。

- history_offset=0（默认）行为与旧版完全一致（最近 history_limit 条）
- history_offset>0 从更早历史返回下一段
- history_has_more / history_offset 字段存在且语义正确
"""
import os, sys
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock


def _make_result(**kw):
    """构造 get_pipeline 返回 dict（绕过真实 DB，直接测截断语义可用源码级检查 + 单元级 mock）"""
    return kw


class TestHistoryOffsetSource:
    """源码级：pipeline.py 应包含 history_offset / history_has_more 返回字段"""

    def test_source_contains_offset_fields(self):
        p = os.path.join(os.path.dirname(__file__), "..",
                         "src", "api", "routes", "pipeline.py")
        src = open(p, encoding="utf-8").read()
        assert "history_offset: int = Query(0" in src, "缺少 history_offset 查询参数"
        assert '"history_offset": _offset' in src, "返回体缺少 history_offset 字段"
        assert '"history_has_more": _has_more' in src, "返回体缺少 history_has_more 字段"

    def test_source_offset_clamped(self):
        p = os.path.join(os.path.dirname(__file__), "..",
                         "src", "api", "routes", "pipeline.py")
        src = open(p, encoding="utf-8").read()
        assert "_offset = min(max(int(history_offset or 0), 0), history_total)" in src, \
            "history_offset 应被 clamp 到 [0, history_total]"


class TestHistorySlicingSemantics:
    """单元级：直接构造 pipelines 列表验证切片语义（与源码同式）"""

    def _slice(self, n_pipelines, history_limit, history_offset):
        pipelines = list(range(n_pipelines))  # 用数字代替 pipeline dict
        active = pipelines[0] if pipelines else None
        history_total = len(pipelines) - 1 if len(pipelines) > 1 else 0
        _offset = min(max(int(history_offset or 0), 0), history_total)
        _h_start = 1 + _offset
        _h_end = _h_start + history_limit
        history = pipelines[_h_start:_h_end] if len(pipelines) > 1 else []
        _has_more = (_offset + history_limit) < history_total
        return active, history, history_total, _has_more

    def test_default_offset_zero_unchanged(self):
        # 584 条 → active 1 + 最近 50 条，与旧行为一致
        active, history, total, more = self._slice(584, 50, 0)
        assert active == 0
        assert len(history) == 50
        assert total == 583
        assert more is True

    def test_offset_returns_older_segment(self):
        # offset=50 → 第 51~150 条更早历史
        active, history, total, more = self._slice(584, 100, 50)
        assert len(history) == 100
        assert history[0] == 51  # pipelines[1+50]
        assert more is True

    def test_offset_beyond_total_clamped_and_exhausted(self):
        # offset 超过 history_total → clamp 到 history_total，history 空，无更多
        active, history, total, more = self._slice(584, 50, 10000)
        assert len(history) == 0
        assert more is False

    def test_last_segment_has_more_false(self):
        # 584 条：offset=533 + limit=50 覆盖 534~583，正好取完
        active, history, total, more = self._slice(584, 50, 533)
        assert len(history) == 50
        assert more is False

    def test_exact_fit_has_more_false(self):
        # total=150，offset=50 limit=100 → 5..150? active=1条，history=150条: offset 50 → 条目 52~150 共99
        active, history, total, more = self._slice(151, 100, 50)
        # offset+limit = 150 == history_total(150) → not <, has_more=False
        assert more is False
        assert len(history) == 100


class TestFrontendIncremental:
    """源码级：pipeline.js 应包含增量拉取逻辑"""

    def test_pipeline_js_incremental_fetch(self):
        p = os.path.join(os.path.dirname(__file__), "..",
                         "src", "api", "static", "js", "pages", "pipeline.js")
        src = open(p, encoding="utf-8").read()
        assert "_pipeEnsurePage" in src, "缺少增量拉取函数"
        assert "history_offset=" in src, "前端应按 history_offset 增量拉取"
        assert "history_has_more" in src, "前端应消费 history_has_more"
        assert "history_total" in src, "分页总数应使用全量 history_total"
        # 分页总数：无过滤时用 history_total（含未加载部分）
        assert "data.history_total != null" in src

    def test_locales_loading_more(self):
        for lang in ("zh", "en"):
            p = os.path.join(os.path.dirname(__file__), "..",
                             "src", "api", "static", "locales", lang, "pipeline.json")
            import json
            d = json.load(open(p, encoding="utf-8"))
            assert d.get("loading_more"), f"{lang} 缺少 loading_more 词条"
