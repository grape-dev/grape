"""worker 的两种心跳不得互相覆盖指标。

worker 有两个心跳循环：完整心跳带 metrics，`_presence_heartbeat_loop` 的最小在线态
心跳**不带**。旧实现把 body 缺省值当 0.0 并无条件写库，于是最小心跳每 15~20s 就把
真实 cpu/mem/disk 刷成 0 —— 看板几秒内「有数据 → 全 0 → 有数据」反复跳（实测：
一台 dev worker 在 4.9% 与 0.0% 之间来回）。

本例锁住服务端契约：
- 不带 metrics 的心跳只刷新 last_seen/last_ip；
- 带 metrics 的心跳照常写入（包括「真的是 0」的采样）；
- v2 心跳（委托 v1）同一语义。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock
import probe_state_support as pss


def _beat(cid, **metrics):
    from src.domains.runtime.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(**metrics)
    return asyncio.run(runtime_heartbeat(body, pss.FakeRequest(), credential_id=cid))


def _metrics(cid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT cpu_percent, memory_percent, disk_percent, tmp_percent, spec, last_seen_at "
            "FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
    return tuple(row) if row else None


@pytest.fixture()
def cid():
    _cid = pss.insert_credential("hbmetrics")
    yield _cid
    pss.cleanup(_cid)


def test_presence_heartbeat_keeps_last_metrics(cid):
    _beat(cid, cpu_percent=12.5, memory_percent=41.2, disk_percent=73.0,
          tmp_percent=14.5, spec="4C7G")
    assert _metrics(cid)[:5] == (12.5, 41.2, 73.0, 14.5, "4C7G")

    # 最小在线态心跳：不带任何 metrics
    before_seen = _metrics(cid)[5]
    _beat(cid)
    after = _metrics(cid)
    assert after[:5] == (12.5, 41.2, 73.0, 14.5, "4C7G"), \
        "不带指标的心跳不得把已有采样覆盖成 0（看板来回跳的根因）"
    assert after[5] and after[5] >= before_seen, "last_seen 仍要被刷新"


def test_reported_zero_is_written(cid):
    """显式上报 0.0 是有效采样（真空闲 CPU），必须落库。"""
    _beat(cid, cpu_percent=12.5, memory_percent=41.2)
    _beat(cid, cpu_percent=0.0, memory_percent=0.0)
    assert _metrics(cid)[:2] == (0.0, 0.0)


def test_partial_metrics_only_update_reported_fields(cid):
    _beat(cid, cpu_percent=5.0, memory_percent=30.0, disk_percent=60.0)
    _beat(cid, cpu_percent=7.0)  # 只报 cpu
    assert _metrics(cid)[:3] == (7.0, 30.0, 60.0)


def test_v2_heartbeat_same_semantics(cid):
    from src.domains.runtime.routes.runtime import runtime_heartbeat_v2, HeartbeatBodyV2
    _beat(cid, cpu_percent=9.0, memory_percent=22.0)
    body = HeartbeatBodyV2(runtime_ids=[])
    asyncio.run(runtime_heartbeat_v2(body, pss.FakeRequest(), credential_id=cid))
    assert _metrics(cid)[:2] == (9.0, 22.0), "v2 委托 v1，不得覆盖成 0"
