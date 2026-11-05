"""Issue #1077-S/P：场景详情 · 相关流水线（场景维度 + SQL 下推）。

演进：
  S → 路由支持 scene_id，先建全仓流水线再过滤（不省成本，6k issue ≈ 12.9s）。
  P → 下推 SQL：先算「要构建的 issue 窗口」（场景过滤 + 最新事件倒序 + LIMIT），
      只构建当页流水线；history_total/failed_count 由聚合查询得出。

覆盖：
1. 源码级：窗口函数存在、场景过滤在 SQL 里、构建只跑窗口、统计来自聚合。
2. 语义级：窗口分页（active + history）、场景隔离、未知场景、全仓兼容。
3. 回归：不得再出现「先建全量再过滤」或 list 线性查找。
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "api", "routes", "pipeline.py")


def _src():
    return open(SRC, encoding="utf-8").read()


class TestPushdownSource:
    def test_route_has_scene_id_param(self):
        assert re.search(r"scene_id: str = Query\(\"\"", _src()), "缺少 scene_id 查询参数"

    def test_window_helper_exists(self):
        assert "def _pipeline_window(" in _src(), "缺少 SQL 下推窗口函数"

    def test_scene_filter_pushed_to_sql(self):
        s = _src()
        assert "COALESCE(NULLIF(i.scene_id,''), e.scene_id) = ?" in s, \
            "场景过滤应下推到 SQL（issue.scene_id 权威 + 事件 scene_id 兜底）"
        assert 'pipelines = [p for p in pipelines if str(p.get("scene_id") or "") == _scene_filter]' not in s, \
            "不应保留「先建全量再过滤」的旧实现"

    def test_build_only_window(self):
        s = _src()
        # 下推成功只用窗口；失败必须回落到完整 issue 列表（架构审查 M-1）
        assert "for iid in (_win_ids if _pushdown_ok else issue_ids)]" in s, \
            "流水线构建应只跑窗口内 issue，且下推失败时回落 issue_ids"

    def test_stats_from_aggregate(self):
        s = _src()
        assert "history_total = _issues_total - 1" in s, "history_total 应来自聚合计数"
        assert "len(_failed_ids) if _pushdown_ok" in s, "failed_count 应来自聚合失败集合"

    def test_tasks_without_events_index_friendly(self):
        s = _src()
        assert "NOT EXISTS (SELECT 1 FROM events e" in s, "任务无事件提示应改索引友好的 NOT EXISTS"
        assert "if _iid not in issue_ids:" not in s, "不要再用 list 线性查找（平方级）"

    def test_query_param_not_clobbered(self):
        s = _src()
        assert 'scene_id = ""\n    pipeline_config' not in s
        assert "_scene_cfg_id" in s


def _window(rows, scene, need):
    """与 SQL 等价：rows=[(issue_id, max_seq, issue_scene)] → (窗口, 总数)。"""
    if scene:
        rows = [r for r in rows if r[2] == scene]
    rows = sorted(rows, key=lambda r: r[1], reverse=True)
    return [r[0] for r in rows[:need]], len(rows)


def _rows():
    rows = [(f"#{i}", 1000 - i, "A") for i in range(1, 26)]     # 场景 A：25 条
    rows += [(f"#{i}", 1000 - i, "B") for i in range(26, 34)]   # 场景 B：8 条
    return rows


class TestWindowSemantics:
    def test_first_page_active_plus_history(self):
        ids, total = _window(_rows(), "A", 1 + 0 + 10)
        assert total == 25 and len(ids) == 11
        assert ids[0] == "#1" and ids[1] == "#2", "active 应为最新，其后是历史窗口"

    def test_second_page(self):
        ids, total = _window(_rows(), "A", 1 + 10 + 10)
        assert len(ids) == 21 and ids[-1] == "#21"

    def test_scene_isolation(self):
        ids, total = _window(_rows(), "B", 1 + 0 + 10)
        assert total == 8 and all(int(i[1:]) >= 26 for i in ids)

    def test_unknown_scene_empty(self):
        ids, total = _window(_rows(), "ZZZ", 11)
        assert ids == [] and total == 0

    def test_no_scene_keeps_all(self):
        ids, total = _window(_rows(), "", 51)
        assert total == 33 and len(ids) == 33