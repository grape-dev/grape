"""Issue #544 L1：SLO 可用率 raw-scan 回填异步化（移出 API 进程）。

覆盖（见 docs/issue-544/plan.md L1）：
- rollup 缺失闭合小时 → 评估不再同步 raw-scan（_availability_raw_buckets 零调用），
  结果透出 `avail_backfill_pending_hours`；
- 完全无 rollup 行（升级/停机窗口）→ 同样转后台，不做全窗同步扫描；
- 后台回填 worker 补齐缺失小时（含零样本小时写 total=0 覆盖标记），补齐后 pending=0。

测试 seam：`slo.compute()` 输出 + `obs_metric_rollup` 行（公开行为，不测内部队列细节）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.observability import metric_rollup as MR  # noqa: E402
from src.observability import slo as S  # noqa: E402
from src.observability import slo_backfill as SB  # noqa: E402
from src.session_manager import db as _db, db_lock as _db_lock  # noqa: E402

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
END = NOW                                   # 对齐小时边界：无头部小段、无当前未闭合桶
START = END - timedelta(hours=48)
WINDOW_HOURS = 48


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_metric_rollup WHERE metric LIKE 'engine_avail_%'",
                    "DELETE FROM obs_runtime_samples WHERE runtime_id BETWEEN 9901 AND 9910"):
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    SB.reset()
    yield
    SB.reset()
    _clean()


def _slo() -> dict:
    return {"id": 99001, "name": "__t_avail", "sli_type": S.SLI_AVAIL,
            "target": 0.9, "window_days": 2, "scope_type": "global",
            "scope_key": "", "created_at": MR._iso(START)}


def _cover(hours_ago: int, n: int, good: float = 9.0, total: float = 10.0):
    """写 n 个已覆盖的小时桶（从 hours_ago 小时前起，向更近处推进）。"""
    for i in range(n):
        b = END - timedelta(hours=hours_ago - i)
        MR.write_bucket("hour", b, {MR.DIM_GLOBAL: {
            "engine_avail_online": (good, int(total)),
            "engine_avail_total": (total, int(total))}})


def _seed_samples(hour_ago: int, online: int, total: int):
    b = END - timedelta(hours=hour_ago)
    for i in range(total):
        with _db_lock:
            _db.cursor().execute(
                "INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at, online)"
                " VALUES (9901, 't', ?, ?)",
                (MR._iso(b + timedelta(minutes=i)), 1 if i < online else 0))


def _raw_calls(monkeypatch):
    calls = []
    orig = S._availability_raw_buckets
    def spy(start, end):
        calls.append((start, end))
        return orig(start, end)
    monkeypatch.setattr(S, "_availability_raw_buckets", spy)
    return calls


# ---------------------------------------------------------------- 部分回填期

def test_partial_backfill_no_sync_raw_scan(monkeypatch):
    """缺失闭合小时：评估不同步 raw-scan，pending 透出，后台补齐。"""
    _cover(48, 24)                       # 近 24 小时有 rollup，远 24 小时缺失
    calls = _raw_calls(monkeypatch)
    out = S.compute(_slo(), ["global"], now=END, allow_engine=True, with_burn=False)
    assert out.get("avail_backfill_pending_hours") == 24, "缺失 24 个闭合小时应透出 pending"
    assert calls == [], "API 评估路径不得对缺失闭合小时做同步 raw-scan（Issue #544 L1）"
    assert out["buckets"] == 24          # 只有已覆盖小时进预算
    # 后台补齐（同步 drain 模拟 worker）
    SB.drain_for_test()
    calls.clear()
    out2 = S.compute(_slo(), ["global"], now=END, allow_engine=True, with_burn=False)
    assert out2.get("avail_backfill_pending_hours", 0) == 0, "回填后不再 pending"
    assert out2["buckets"] == 24         # 零样本小时 total=0 行不进预算（口径不变）
    assert calls == [], "回填后评估仍不应触发 raw-scan"


def test_backfill_writes_zero_sample_hours_and_real_samples(monkeypatch):
    """零样本小时写 total=0 覆盖行；有样本小时聚合出真实值。"""
    _cover(48, 24)
    _seed_samples(10, online=6, total=10)   # 缺失段内某小时有 10 采样 6 在线
    SB.schedule(END - timedelta(hours=24), END)
    SB.drain_for_test()
    with _db_lock:
        c = _db.cursor()
        rows = dict((r[0], (float(r[1] or 0), int(r[2] or 0))) for r in c.execute(
            "SELECT bucket_start, value, cnt FROM obs_metric_rollup"
            " WHERE resolution='hour' AND metric='engine_avail_total' AND dim_key='global'"))
    assert len(rows) == 48, "全部闭合小时都应有覆盖行（含零样本小时）"
    b30 = MR._iso(END - timedelta(hours=10))
    assert rows[b30] == (10.0, 10), "有样本小时应聚合出 SUM(online)/COUNT"
    zero_hours = [b for b, (v, n) in rows.items() if n == 0]
    assert zero_hours, "零样本小时应有 total=0 覆盖标记行"
    b_on = dict((r[0], float(r[1] or 0)) for r in _q(
        "SELECT bucket_start, value FROM obs_metric_rollup"
        " WHERE resolution='hour' AND metric='engine_avail_online' AND dim_key='global'"))
    assert b_on[b30] == 6.0


def _q(sql):
    with _db_lock:
        return list(_db.cursor().execute(sql))


# ---------------------------------------------------------------- 全窗无 rollup

def test_no_rollup_rows_goes_background(monkeypatch):
    """升级/停机窗口（一行 engine_avail 都没有）：不做全窗同步扫描，转后台。"""
    calls = _raw_calls(monkeypatch)
    out = S.compute(_slo(), ["global"], now=END, allow_engine=True, with_burn=False)
    assert out.get("avail_backfill_pending_hours") == WINDOW_HOURS
    assert calls == [], "无 rollup 行时不得全窗同步 raw-scan（Issue #544 L1）"
    assert out["state"] == S.STATE_NO_DATA
    SB.drain_for_test()
    out2 = S.compute(_slo(), ["global"], now=END, allow_engine=True, with_burn=False)
    assert out2.get("avail_backfill_pending_hours", 0) == 0


def test_head_and_current_hour_bounded_scan_kept(monkeypatch):
    """头部不完整小时/当前未闭合小时的有界小段现算保留（分钟级，非阻塞源）。"""
    _cover(24, 24)
    calls = _raw_calls(monkeypatch)
    now = END + timedelta(minutes=30)          # 当前小时进行中
    slo = _slo()
    slo["created_at"] = MR._iso(END - timedelta(hours=24, minutes=30))  # 头部小段 30min
    out = S.compute(slo, ["global"], now=now, allow_engine=True, with_burn=False)
    bounded = [c for c in calls if (c[1] - c[0]) <= timedelta(hours=1)]
    assert calls == bounded, "只允许 ≤1h 的有界小段现算"
    assert out["buckets"] >= 24
