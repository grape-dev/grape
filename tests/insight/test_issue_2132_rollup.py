"""Issue #2132 A1/A2 回归（rollup 域内）：谓词下推 + 锁外聚合 + 真实聚合有效性。

F1 教训：A2 把循环体移出锁时若缩进错位，聚合代码会整体落进 `if ...: continue`
之后成为不可达代码——接口 200 且结构完整、曲线全空。这里用**真实喂行**断言
聚合结果非 None，缩进错位立即红。
"""
import sys
import types
from datetime import datetime, timedelta, timezone

from src.domains.insight.observability import rollup


class _FakeCursor:
    def __init__(self):
        self.sqls = []

    def execute(self, sql, params=()):
        self.sqls.append((sql, params))
        return self

    def fetchall(self):
        return []


class _FakeDB:
    def __init__(self, cursor):
        self._cur = cursor

    def cursor(self):
        return self._cur


class _LockRecorder:
    """with db_lock: 语句块内执行的 SQL 全部记入 locked_sqls。"""

    def __init__(self, cursor):
        self._cur = cursor
        self.locked_sqls = []
        self._depth = 0

    def __enter__(self):
        self._depth += 1
        return self

    def __exit__(self, *a):
        self._depth -= 1
        if self._depth == 0:
            self.locked_sqls = list(self._cur.sqls)
        return False


def _install(monkeypatch, db, lock):
    holder = types.ModuleType("src.kernel.owner_db")
    holder.db_pair = lambda dom: (db, lock)
    monkeypatch.setitem(sys.modules, "src.kernel.owner_db", holder)


def test_runtime_history_pushes_down_cutoff_for_both_formats(monkeypatch):
    """A1：谓词对 ISO 与裸格式两个起点同时下推（两态字典序不能只喂一个串）。"""
    cur = _FakeCursor()
    lock = _LockRecorder(cur)
    _install(monkeypatch, _FakeDB(cur), lock)

    rollup.runtime_history([1, 2], range_str="6h")
    sql = lock.locked_sqls[0][0] if lock.locked_sqls else None
    assert sql, "没有 SQL 落到锁内"
    assert "sampled_at >= ? OR sampled_at >= ?" in sql, sql
    assert "WHERE runtime_id IN (?,?)" in sql, sql
    # 参数 = 两个 runtime id + 两个起点（ISO 与裸格式）
    params = lock.locked_sqls[0][1]
    assert len(params) == 4, params
    assert "T" in params[2], "ISO 起点"
    assert " " in params[3] and "T" not in params[3], "裸格式起点"


def test_runtime_history_aggregates_rows(monkeypatch):
    """喂入窗口内 1 行采样，聚合出的桶值必须非 None（钉死 F1 类缩进错位）。"""
    now = datetime.now(timezone.utc)
    sampled = (now - timedelta(minutes=5)).isoformat()
    rows = [(1, sampled, 10.0, 20.0, 30.0, 90)]

    class _Cur(_FakeCursor):
        def fetchall(self):
            return rows

    cur = _Cur()
    lock = _LockRecorder(cur)
    _install(monkeypatch, _FakeDB(cur), lock)

    data = rollup.runtime_history([1], range_str="6h")
    cpu = data["series"]["1"]["cpu"]
    assert any(v is not None for v in cpu), cpu  # 有真实数据点，不再恒真


def test_runtime_history_aggregates_outside_lock(monkeypatch):
    """A2：fetchall 只发生在锁内；行处理在锁释放后（fetchall 断言在锁内成立）。"""
    now = datetime.now(timezone.utc)
    rows = [(1, (now - timedelta(minutes=5)).isoformat(), 10.0, 20.0, 30.0, 90)]
    phase = {"in_lock": False}

    class _Cur:
        def execute(self, sql, params=()):
            return self

        def fetchall(self):
            assert phase["in_lock"], "fetchall 必须发生在锁内"
            return rows

    cur = _Cur()

    class _Guard:
        def __enter__(g):
            phase["in_lock"] = True
            return cur

        def __exit__(g, *a):
            phase["in_lock"] = False
            return False

    class _DB:
        def cursor(self):
            return cur

    _install(monkeypatch, _DB(), _Guard())
    data = rollup.runtime_history([1], range_str="6h")
    assert any(v is not None for v in data["series"]["1"]["cpu"])
