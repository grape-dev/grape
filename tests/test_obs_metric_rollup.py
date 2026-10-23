"""业务指标聚合表测试（P1-2，§6.2）。

钉死五件事：
1. **分桶口径**：计数/成功率/排队等待/耗时 avg+p95/token 求和都对；维度含各仓库 + `global`
2. **时间戳格式混用**（真实的坑）：`agent_tasks.*` 是 ISO(UTC)，`obs_llm_usage.created_at` 是
   数据库本地串（`datetime('now')`）——两者都要能落进正确的桶
3. **幂等**：同一桶重复写不产生重复行（UNIQUE + upsert）；**追赶补齐**只补缺的桶
4. **读接口**：序列聚合口径（求和 / 按 cnt 加权 / p95 取最大）；**维度为空不降级成全量**
5. **留存**：只清超过 7 天（分钟）/ 30 天（小时）的行
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import metric_rollup as mr   # noqa: E402

UID = 7811
REPO = "gitcode:org/roll-mine"
OTHER = "gitcode:org/roll-theirs"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _local(dt):
    """模拟 MySQL `datetime('now')` 写出来的本地时间串（CST）。"""
    return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_metric_rollup",
                    "DELETE FROM agent_tasks WHERE id LIKE 't_rl_%'",
                    "DELETE FROM obs_llm_usage WHERE task_id LIKE 't_rl_%'",
                    "DELETE FROM obs_runtime_samples WHERE runtime_id BETWEEN 9901 AND 9930"):
            try:
                c.execute(sql)
            except Exception:
                pass
        try:
            c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        except Exception:
            pass
        db.commit()


def _mk_task(tid, repo_key, status, created, started=None, completed=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key,"
                  " agent_type, user_id, status, created_at, started_at, completed_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "s-" + tid, 1, "p", repo_key, "coding", UID, status,
                   _iso(created), _iso(started) if started else None,
                   _iso(completed) if completed else None))
        db.commit()


def _mk_usage(tid, repo_key, created, tokens=100, local_fmt=True):
    """插一条用量行；local_fmt=True 用本地时间串（与 DDL 默认一致）。"""
    from src.session_manager import db, db_lock
    ts = _local(created) if local_fmt else _iso(created)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_llm_usage (dedup_key, task_id, seq, turn, source, repo_key,"
                  " agent_type, engine_id, user_id, provider_raw, model_raw, model_key,"
                  " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
                  " reasoning_tokens, cost_status, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"rl-{tid}", tid, 1, 1, "hermes", repo_key, "coding", "1", UID, "anthropic",
                   "claude", "claude-sonnet-4.5", tokens, 10, 5, 1, 2, "unknown", ts))
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------- 分桶计算

def test_compute_bucket_engine_availability_preagg():
    """Issue #437：小时桶补引擎可用率预聚合（engine_avail_online/total，仅 global 维）。"""
    from src.session_manager import db, db_lock
    base = _now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    with db_lock:
        c = db.cursor()
        for k in range(60):
            ts = _iso(base + timedelta(seconds=k * 60))
            c.execute("INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at,"
                      " online) VALUES (9911, 'roll-dev', ?, ?)", (ts, 1 if k < 40 else 0))
        db.commit()
    rows = mr.compute_bucket(base, base + timedelta(hours=1))
    g = rows[mr.DIM_GLOBAL]
    assert g["engine_avail_online"] == (40.0, 60), g
    assert g["engine_avail_total"] == (60.0, 60), g
    # 空桶（无采样）不写 availability 指标
    rows2 = mr.compute_bucket(base + timedelta(hours=1), base + timedelta(hours=2))
    assert "engine_avail_online" not in rows2.get(mr.DIM_GLOBAL, {}), rows2.get(mr.DIM_GLOBAL)


def test_compute_bucket_counts_rate_and_latency():
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    _mk_task("t_rl_ok", REPO, "completed", base + timedelta(seconds=1),
             base + timedelta(seconds=3), base + timedelta(seconds=13))
    _mk_task("t_rl_bad", REPO, "failed", base + timedelta(seconds=2),
             base + timedelta(seconds=12), base + timedelta(seconds=32))
    _mk_usage("t_rl_ok", REPO, base + timedelta(seconds=40), tokens=1000)
    _mk_usage("t_rl_bad", REPO, base + timedelta(seconds=50), tokens=2000)

    rows = mr.compute_bucket(base, base + timedelta(minutes=1))
    m = rows[mr._repo_dim(REPO)]
    assert m["task_enqueued"] == (2.0, 2)
    assert m["task_done"] == (1.0, 1) and m["task_failed"] == (1.0, 1)
    assert m["task_success_rate"] == (50.0, 2)
    # 排队等待 = started - created = 2s 与 10s → avg 6
    assert m["task_queue_wait_avg"][0] == pytest.approx(6.0, abs=0.01)
    # 执行耗时 = 10s 与 20s → avg 15，p95 落在两者之间
    assert m["task_duration_avg"][0] == pytest.approx(15.0, abs=0.01)
    assert 10 <= m["task_duration_p95"][0] <= 20
    # token：input 求和；output = output + reasoning
    assert m["token_input"][0] == pytest.approx(3000.0)
    assert m["token_output"][0] == pytest.approx(24.0)      # (10+2)*2
    assert m["token_cache"][0] == pytest.approx(12.0)       # (5+1)*2
    assert m["token_total"][0] == pytest.approx(3036.0)
    # global 汇总（其它仓库此刻没有数据 → 与 repo 相同 + 在线数）
    g = rows[mr.DIM_GLOBAL]
    assert g["task_enqueued"][0] == 2.0 and g["task_success_rate"][0] == 50.0
    assert "runtimes_online" in g
    # 其它维度不受影响：既没建仓库行，也不把别人算进来
    assert mr._repo_dim(OTHER) not in rows


def test_compute_bucket_handles_usage_local_timestamp():
    """token 行是 DB 本地时间串 → 仍要落进正确桶（格式混用的真实坑）。"""
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=3)
    _mk_usage("t_rl_loc", REPO, base + timedelta(seconds=10), tokens=777, local_fmt=True)
    rows = mr.compute_bucket(base, base + timedelta(minutes=1))
    assert rows[mr._repo_dim(REPO)]["token_input"][0] == pytest.approx(777.0)
    # 桶外的不算
    rows2 = mr.compute_bucket(base + timedelta(minutes=1), base + timedelta(minutes=2))
    assert mr._repo_dim(REPO) not in rows2
    # ISO 形态也要能算（万一有别的写入路径）
    _mk_usage("t_rl_iso", REPO, base + timedelta(seconds=20), tokens=111, local_fmt=False)
    rows3 = mr.compute_bucket(base, base + timedelta(minutes=1))
    assert rows3[mr._repo_dim(REPO)]["token_input"][0] == pytest.approx(888.0)


def test_write_bucket_is_idempotent():
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=10)
    _mk_task("t_rl_idem", REPO, "completed", base, base + timedelta(seconds=5),
             base + timedelta(seconds=20))
    rows = mr.compute_bucket(base, base + timedelta(minutes=1))
    n1 = mr.write_bucket(mr.RES_MINUTE, base, rows)
    n2 = mr.write_bucket(mr.RES_MINUTE, base, rows)      # 再写一次
    assert n1 > 0 and n2 == n1
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_metric_rollup WHERE resolution=? AND bucket_start=?",
                  (mr.RES_MINUTE, _iso(base)))
        assert c.fetchone()[0] == len(
            [1 for _dim, mm in rows.items() for _m in mm]), "重复写产生了重复行"


# ---------------------------------------------------------------- 追赶补齐

def test_catch_up_fills_missing_and_skips_existing():
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=3)
    _mk_task("t_rl_catch", REPO, "completed", base, base + timedelta(seconds=2),
             base + timedelta(seconds=30))
    first = mr.catch_up(now=base + timedelta(minutes=3), minute_back_min=10, hour_back_hours=0,
                        max_minutes=10)
    assert first["minute_done"] > 0, first
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT DISTINCT bucket_start FROM obs_metric_rollup WHERE resolution=?",
                  (mr.RES_MINUTE,))
        _ = {r[0] for r in c.fetchall()}
    # 再跑一次：已有的桶不再重算（幂等 + 省算力）
    second = mr.catch_up(now=base + timedelta(minutes=3), minute_back_min=10, hour_back_hours=0,
                         max_minutes=10)
    assert second["minute_done"] == 0, second
    # 当前分钟（未结束）不写
    cur = mr.floor_bucket(_now(), mr.RES_MINUTE)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_metric_rollup WHERE bucket_start=?", (_iso(cur),))
        assert c.fetchone()[0] == 0, "未结束的桶不该写"


def test_catch_up_hour_resolution():
    base = _now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _mk_task("t_rl_hour", REPO, "completed", base + timedelta(minutes=10),
             base + timedelta(minutes=11), base + timedelta(minutes=40))
    stats = mr.catch_up(now=base + timedelta(hours=2), minute_back_min=0, hour_back_hours=6,
                        max_hours=6)
    assert stats["hour_done"] > 0, stats
    got = mr.series(["task_done"], [mr._repo_dim(REPO)], base, base + timedelta(hours=1),
                    mr.RES_HOUR)
    assert got["task_done"] and got["task_done"][0]["value"] == 1.0, got


# ---------------------------------------------------------------- 读接口

def test_series_aggregation_over_dims():
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=20)
    rows_a = {mr._repo_dim(REPO): {"task_done": (3.0, 3), "task_success_rate": (100.0, 3),
                                   "task_duration_p95": (10.0, 3)},
              mr.DIM_GLOBAL: {"task_done": (3.0, 3)}}
    rows_b = {mr._repo_dim(OTHER): {"task_done": (1.0, 1), "task_success_rate": (50.0, 1),
                                    "task_duration_p95": (99.0, 1)}}
    mr.write_bucket(mr.RES_MINUTE, base, rows_a)
    mr.write_bucket(mr.RES_MINUTE, base, rows_b)
    got = mr.series(["task_done", "task_success_rate", "task_duration_p95"],
                    [mr._repo_dim(REPO), mr._repo_dim(OTHER)], base, base, mr.RES_MINUTE)
    assert got["task_done"][0]["value"] == 4.0, "求和类应相加"
    # 加权平均：(100*3 + 50*1) / 4 = 87.5
    assert got["task_success_rate"][0]["value"] == pytest.approx(87.5, abs=0.01)
    assert got["task_duration_p95"][0]["value"] == 99.0, "p95 合并取最大（保守）"
    # 维度为空 → 空序列（绝不降级成全量）
    assert mr.series(["task_done"], [], base, base, mr.RES_MINUTE)["task_done"] == []
    assert not mr.series(["task_done"], ["repo:gitcode:org/nope"], base, base,
                         mr.RES_MINUTE)["task_done"]


def test_overview_trends_from_rollup_and_fallback():
    from src.observability import overview
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=10)
    mr.write_bucket(mr.RES_MINUTE, base, {mr._repo_dim(REPO): {
        "task_done": (2.0, 2), "task_failed": (1.0, 1), "token_input": (500.0, 2)}})
    out = mr.overview_trends([mr._repo_dim(REPO)], base, 60, 3)
    assert out is not None
    assert sum(out["task_outcome"]["completed"]) == 2
    assert sum(out["task_outcome"]["failed"]) == 1
    assert out["task_outcome"]["rate"][0] == pytest.approx(66.7, abs=0.1)
    assert sum(out["token_trend"]["input"]) == 500
    # 维度为空/无数据 → None（调用方回退直查）
    assert mr.overview_trends([], base, 60, 3) is None
    assert mr.overview_trends(["repo:gitcode:org/void"], base, 60, 3) is None
    # 总览接口：有 rollup 数据 → charts.source=rollup；清掉 → live
    from src.session_manager import db, db_lock
    _grant(REPO)
    user = {"id": UID, "username": "roll-tester", "org_id": 0, "org_role": "owner"}
    ov = overview.build_overview(user, range_str="1h")
    assert ov["charts"]["source"] == "rollup", ov["charts"]["source"]
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM obs_metric_rollup")
        db.commit()
    ov2 = overview.build_overview(user, range_str="1h")
    assert ov2["charts"]["source"] == "live", "无 rollup 数据应回退直查"


def _grant(repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin,"
                  " is_join) VALUES (?,?,?,?,1,1)", (UID, repo_key, owner, name))
        db.commit()


# ---------------------------------------------------------------- 留存

def test_unattributed_usage_counts_in_global_only():
    """主控机内跑的引擎（chat/im/wiki，无 repo_key）的用量：**进 global，不冒空仓库维度**。"""
    _clean()
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    _mk_usage("t_rl_unattr", "", base + timedelta(seconds=5), tokens=1000)
    b = mr.compute_bucket(base, base + timedelta(minutes=1))
    dims = sorted(b.keys())
    assert mr.DIM_GLOBAL in dims, dims
    assert "repo:" not in dims, f"不许出现空仓库维度: {dims}"
    assert b[mr.DIM_GLOBAL]["token_input"][0] == 1000.0, b[mr.DIM_GLOBAL]


def test_cleanup_only_removes_expired():
    now = _now()
    fresh = mr.floor_bucket(now - timedelta(days=1), mr.RES_MINUTE)
    old_min = mr.floor_bucket(now - timedelta(days=8), mr.RES_MINUTE)
    fresh_h = mr.floor_bucket(now - timedelta(days=10), mr.RES_HOUR)
    old_h = mr.floor_bucket(now - timedelta(days=31), mr.RES_HOUR)
    for res, b in ((mr.RES_MINUTE, fresh), (mr.RES_MINUTE, old_min),
                   (mr.RES_HOUR, fresh_h), (mr.RES_HOUR, old_h)):
        mr.write_bucket(res, b, {"global": {"task_done": (1.0, 1)}})
    got = mr.cleanup(now)
    assert got["minute"] >= 1 and got["hour"] >= 1, got
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_metric_rollup WHERE resolution=? AND bucket_start=?",
                  (mr.RES_MINUTE, _iso(fresh)))
        assert c.fetchone()[0] == 1, "未过期的分钟行被误删"
        c.execute("SELECT COUNT(*) FROM obs_metric_rollup WHERE resolution=? AND bucket_start=?",
                  (mr.RES_HOUR, _iso(fresh_h)))
        assert c.fetchone()[0] == 1, "未过期的小时行被误删"


# ---------------------------------------------------------------- 路由

def test_l1_and_l2_series_routes():
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin, require_org_member
    from src.api.main import app

    _grant(REPO)
    base = _now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    mr.write_bucket(mr.RES_MINUTE, base, {
        mr._repo_dim(REPO): {"task_done": (2.0, 2), "task_success_rate": (100.0, 2)},
        mr._repo_dim(OTHER): {"task_done": (9.0, 9), "task_success_rate": (10.0, 9)},
        mr.DIM_GLOBAL: {"task_done": (11.0, 11)}})
    user = {"id": UID, "username": "roll-tester", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/metrics/series", params={"metric": "task_done", "hours": 1})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["global"] is False
            assert body["dim_keys"] == [f"repo:{REPO}"], body["dim_keys"]      # 只含可见仓库
            assert body["series"]["task_done"][-1]["value"] == 2.0, "他人仓库不能被算进来"
            # 未知指标 / 非法分辨率 → 400
            assert cli.get("/api/obs/metrics/series", params={"metric": "nope"}).status_code == 400
            assert cli.get("/api/obs/metrics/series",
                           params={"resolution": "second"}).status_code == 400
            # 越权仓库 → 404
            assert cli.get("/api/obs/metrics/series",
                           params={"repo": "org/roll-theirs"}).status_code == 404
            # L2：global 维度可见（用户面看不到的那个）
            r2 = cli.get("/api/admin/obs/metrics/series",
                         params={"metric": "task_done", "scope": "global", "hours": 1})
            assert r2.status_code == 200, r2.text
            assert r2.json()["dim_keys"] == [mr.DIM_GLOBAL]
            assert r2.json()["series"]["task_done"][-1]["value"] == 11.0
            r3 = cli.get("/api/admin/obs/metrics/series",
                         params={"metric": "task_done", "scope": "repo", "repo": "org/roll-theirs",
                                 "hours": 1})
            assert r3.status_code == 200 and r3.json()["series"]["task_done"][-1]["value"] == 9.0
            assert cli.get("/api/admin/obs/metrics/series",
                           params={"scope": "bogus"}).status_code == 400
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        app.dependency_overrides.pop(require_admin, None)