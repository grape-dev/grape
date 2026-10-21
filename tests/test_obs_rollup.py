"""设备资源留存测试：采样 / 分桶 / 清理 / 用户面可见性（§6 指标留存 + 资源曲线）。

钉死三件事：
1. 采样覆盖所有未吊销引擎、跳过已吊销的；过期采样会被清理（表不会无限增长）
2. 同桶多次采样求平均（不是取最后一条），跨桶不串
3. **越权拿不到**：他人 user-scope 引擎不出现在列表里，指定不可见 runtime_id → 404
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import rollup  # noqa: E402

PREFIX = "obsroll-test-"


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE ?", (PREFIX + "%",))
            c.execute("DELETE FROM obs_runtime_samples WHERE device_name LIKE ?", (PREFIX + "%",))
        except Exception:
            pass
        db.commit()


def _mk_cred(name, scope="global", owner_name="", status="active"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name,"
                  " status, cpu_percent, memory_percent, disk_percent, bandwidth_percent)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("h-" + name, PREFIX + name, PREFIX + name, scope, owner_name, status,
                   12.5, 34.5, 56.5, 1.5))
        db.commit()
        c.execute("SELECT id FROM runtime_credentials WHERE name=?", (PREFIX + name,))
        return int(c.fetchone()[0])


def _sample(runtime_id, device, minutes_ago, cpu, mem=0.0, disk=50.0, health=90, status="online"):
    from src.session_manager import db, db_lock
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at,"
                  " cpu_percent, memory_percent, disk_percent, health_score, status)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (int(runtime_id), device, ts, cpu, mem, disk, health, status))
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


def test_snapshot_writes_all_but_revoked():
    rid_ok = _mk_cred("a")
    rid_revoked = _mk_cred("b", status="revoked")
    r = rollup.snapshot_runtimes()
    assert r["inserted"] >= 1, r
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_runtime_samples WHERE runtime_id=?", (rid_ok,))
        assert c.fetchone()[0] >= 1, "未吊销引擎应有采样"
        c.execute("SELECT COUNT(*) FROM obs_runtime_samples WHERE runtime_id=?", (rid_revoked,))
        assert c.fetchone()[0] == 0, "已吊销引擎不应被采样"


def test_snapshot_prunes_expired_samples():
    rid = _mk_cred("c")
    _sample(rid, PREFIX + "c", minutes_ago=8 * 24 * 60, cpu=99)      # 8 天前 → 应被清掉
    _sample(rid, PREFIX + "c", minutes_ago=5, cpu=11)                 # 保留
    rollup.snapshot_runtimes()
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_runtime_samples WHERE sampled_at < ?",
                  ((datetime.now(timezone.utc) - timedelta(days=rollup.RETENTION_DAYS)).isoformat(),))
        assert c.fetchone()[0] == 0, "过期采样应被清理"


def test_history_buckets_and_averages():
    rid = _mk_cred("d")
    # 6h 窗口、300s 桶：同一桶内两条 → 求平均
    _sample(rid, PREFIX + "d", minutes_ago=3, cpu=10, mem=20)
    _sample(rid, PREFIX + "d", minutes_ago=3, cpu=30, mem=40)
    # 另一个桶
    _sample(rid, PREFIX + "d", minutes_ago=9, cpu=50, mem=60)
    # 窗口外（7h 前）
    _sample(rid, PREFIX + "d", minutes_ago=7 * 60, cpu=99, mem=99)

    d = rollup.runtime_history([rid], range_str="6h")
    assert d["range"] == "6h" and d["bucket_seconds"] == 300
    vals = d["series"][str(rid)]["cpu"]
    got = [v for v in vals if v is not None]
    assert sorted(got) == [20.0, 50.0], f"同桶应求平均且窗口外不计入: {got}"
    mem_got = [v for v in d["series"][str(rid)]["mem"] if v is not None]
    assert sorted(mem_got) == [30.0, 60.0]


def test_history_empty_ids_is_safe():
    d = rollup.runtime_history([], range_str="6h")
    assert d["series"] == {} and len(d["buckets"]) > 0


def test_unknown_range_falls_back():
    d = rollup.runtime_history([], range_str="99y")
    assert d["range"] == rollup.DEFAULT_RANGE


# ---------------------------------------------------------------- 路由 + 可见性

def test_route_lists_only_visible_runtimes_and_404s_others():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    mine = _mk_cred("mine", scope="global")
    others = _mk_cred("others", scope="user", owner_name="someone-else")
    _sample(mine, PREFIX + "mine", minutes_ago=2, cpu=25, mem=35)
    _sample(others, PREFIX + "others", minutes_ago=2, cpu=95, mem=95)

    user = {"id": 5566, "username": "me", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/runtimes/history", params={"range": "6h"})
            assert r.status_code == 200, r.text
            body = r.json()
            ids = [x["id"] for x in body["runtimes"]]
            assert mine in ids, ids
            assert others not in ids, f"他人个人引擎不该出现: {ids}"
            assert str(others) not in body["series"], "他人引擎不该有序列数据"

            # 指定不可见引擎 → 404（不区分不存在/无权限）
            r = cli.get("/api/obs/runtimes/history", params={"runtime_id": others})
            assert r.status_code == 404, r.text
            # 指定可见引擎 → 只回这一台
            r = cli.get("/api/obs/runtimes/history", params={"runtime_id": mine, "range": "6h"})
            assert r.status_code == 200, r.text
            assert [x["id"] for x in r.json()["runtimes"]] == [mine]
            # 越权仓库 → 404
            r = cli.get("/api/obs/runtimes/history", params={"repo": "org/nope"})
            assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.pop(require_org_member, None)