"""Issue #1077-S：场景详情 · 相关流水线按场景独立查询。

背景：旧实现从 `/pipeline/{owner}/{repo}` 的整仓最近 `history_limit`(默认 50) 条里
前端过滤 `scene_id`，仓库越忙本场景越容易显示不全。现在路由支持 `scene_id` 参数，
在构建完 pipelines 后按场景收窄，再切 active/history/统计。

覆盖：
1. 源码级：路由有 scene_id 参数、过滤发生在切片之前、返回体回显生效场景。
2. 语义级：按场景过滤后的 active/history_total/history_has_more 切片正确（含分页）。
3. 兼容：不传 scene_id 时行为与旧版一致（全仓窗口）。
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "api", "routes", "pipeline.py")


def _src():
    return open(SRC, encoding="utf-8").read()


class TestSceneFilterSource:
    def test_route_has_scene_id_param(self):
        s = _src()
        assert re.search(r"scene_id: str = Query\(\"\"", s), "缺少 scene_id 查询参数"

    def test_filter_applied_before_slicing(self):
        s = _src()
        i_filter = s.index('_scene_filter = str(scene_id or "").strip()')
        i_slice = s.index("history_total = len(pipelines) - 1")
        assert i_filter < i_slice, "场景过滤必须发生在 active/history 切片之前"
        assert 'pipelines = [p for p in pipelines if str(p.get("scene_id") or "") == _scene_filter]' in s

    def test_response_echoes_effective_scene(self):
        s = _src()
        assert s.count('"scene_id": _scene_filter,') == 1, "返回体应回显生效的 scene_id（且不影响 _get_issue_pipeline 内部字段）"
        assert '"scene_id": scene_id,' in s, "_get_issue_pipeline 内部字段仍应是局部 scene_id"

    def test_query_param_not_clobbered(self):
        """历史坑：函数内 scene_id = \"\" 占位会覆盖入参，必须已改名。"""
        s = _src()
        assert 'scene_id = ""\n    pipeline_config' not in s
        assert "_scene_cfg_id" in s


def _slice_after_scene_filter(pipelines, scene, history_limit, history_offset):
    """与路由同式的语义：先按场景过滤，再切 active/history/分页。"""
    if scene:
        pipelines = [p for p in pipelines if p.get("scene_id") == scene]
    pipelines.sort(key=lambda p: p["seq"], reverse=True)
    active = pipelines[0] if pipelines else None
    history_total = len(pipelines) - 1 if len(pipelines) > 1 else 0
    _offset = min(max(int(history_offset or 0), 0), history_total)
    _h_start = 1 + _offset
    _h_end = _h_start + history_limit
    history = pipelines[_h_start:_h_end] if len(pipelines) > 1 else []
    has_more = (_offset + history_limit) < history_total
    return active, history, history_total, has_more


def _mk(n, scene, start_seq=1):
    return [{"issue_id": f"#{start_seq + i}", "scene_id": scene, "seq": start_seq + i} for i in range(n)]


class TestSceneFilterSemantics:
    def test_scene_scoped_paging(self):
        pipes = _mk(25, "sceneA") + _mk(8, "sceneB", 100)
        for page in range(3):
            active, history, total, more = _slice_after_scene_filter(pipes, "sceneA", 10, page * 10)
            assert active is not None and active["scene_id"] == "sceneA"
            assert total == 24, "场景 A 共 25 条 → active 1 + history 24"
            if page < 2:
                assert len(history) == 10
                assert more is True
            else:
                assert len(history) == 4
                assert more is False

    def test_other_scene_isolated(self):
        pipes = _mk(25, "sceneA") + _mk(8, "sceneB", 100)
        _, history, total, more = _slice_after_scene_filter(pipes, "sceneB", 10, 0)
        assert total == 7
        assert len(history) == 7 and more is False
        assert all(p["scene_id"] == "sceneB" for p in history)

    def test_unknown_scene_returns_empty(self):
        pipes = _mk(25, "sceneA")
        active, history, total, more = _slice_after_scene_filter(pipes, "nope", 10, 0)
        assert active is None and history == [] and total == 0 and more is False

    def test_no_scene_keeps_legacy_window(self):
        pipes = _mk(120, "sceneA")
        active, history, total, more = _slice_after_scene_filter(pipes, "", 50, 0)
        assert active is not None and active["seq"] == 120
        assert len(history) == 50 and total == 119 and more is True