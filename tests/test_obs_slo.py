"""SLO / 错误预算（P2-1，§6.7）单测。

覆盖：seed 幂等 / 三类 SLI 的桶比例法与预算状态 / L2-only 边界 / 可见范围为空不降级 /
配置校验 / 快照幂等与清理 / 燃尽曲线累计与抽稀。

口径（与 `src/observability/slo.py` 模块注释一致）：
  坏桶 = 桶内自身比例 < target 的小时桶；错误预算 = (1-target) × 有数据的桶数；
  消耗比例 ≥ 0.75 → warning，≥ 1.0 → breached。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.observability import metric_rollup as MR  # noqa: E402
from src.observability import slo as S  # noqa: E402

REPO_DIM = "repo:org/slo-x"
NOW = datetime.now(timezone.utc)


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_metric_rollup WHERE dim_key LIKE 'repo:org/slo-%'",
                    "DELETE FROM obs_metric_rollup WHERE dim_key='global' AND metric LIKE 'task_%'",
                    "DELETE FROM obs_runtime_samples WHERE runtime_id BETWEEN 9901 AND 9910",
                    "DELETE FROM obs_slo_snapshots",
                    "DELETE FROM obs_slo WHERE name LIKE '__t_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    S.seed_defaults()
    yield
    _clean()


def _hour(ago_h: int) -> datetime:
    return MR.floor_bucket(NOW - timedelta(hours=ago_h), "hour")


def _mk_hours(n: int, done: int, failed: int, dim: str = REPO_DIM, bad_hours: int = 0,
              bad_done: int = 90, bad_failed: int = 10):
    """写 n 个小时桶：前 bad_hours 个用坏比例，其余用 done/failed。"""
    for i in range(n):
        if i < bad_hours:
            g, b = bad_done, bad_failed
        else:
            g, b = done, failed
        MR.write_bucket("hour", _hour(i + 1),
                        {dim: {"task_done": (float(g), int(g)),
                               "task_failed": (float(b), int(b))}})


def _slo(name: str) -> dict:
    for s in S.list_slos():
        if s["name"] == name:
            return s
    raise AssertionError(f"missing slo {name}")


# ---------------------------------------------------------------- 配置

def test_seed_defaults_is_idempotent():
    assert len(S.list_slos()) == len(S.DEFAULTS) >= 3
    assert S.seed_defaults() == 0                      # 再跑一次不重复插
    names = [s["name"] for s in S.list_slos()]
    assert names == [d["name"] for d in S.DEFAULTS]
    lat = _slo(S.SLI_LATENCY)
    assert lat["threshold"].get("max_p95_sec") == 300  # 阈值进 threshold_json


def test_update_slo_validates_target_and_caps_window():
    sid = S.create_slo("__t_upd", S.SLI_SUCCESS, target=0.9, window_days=7)
    assert S.update_slo(sid, target=0.995)
    got = S.get_slo(sid) or {}
    assert abs(float(got.get("target") or 0) - 0.995) < 1e-9
    assert S.update_slo(sid, window_days=999)          # 超出上限 → 收到 30
    assert int((S.get_slo(sid) or {}).get("window_days") or 0) == S.MAX_WINDOW_DAYS
    with pytest.raises(ValueError):
        S.update_slo(sid, target=1.5)                  # 非法目标
    assert S.update_slo(sid, nonsense="x") is False     # 白名单外字段
    # 同名幂等
    assert S.create_slo("__t_upd", S.SLI_SUCCESS) == sid


# ---------------------------------------------------------------- 三类 SLI

def test_success_rate_slo_ok_then_breached():
    slo = _slo(S.SLI_SUCCESS)          # target 0.99 / 30 天
    # 100 小时，每小时 99/100 成功 → 恰好达目标（0.99 不小于 target，不算坏桶）
    _mk_hours(100, done=99, failed=1)
    r = S.compute(slo, [REPO_DIM], now=NOW)
    assert r["buckets"] == 100, r
    assert abs(r["value"] - 0.99) < 1e-9, r
    assert r["bad_buckets"] == 0 and r["state"] == S.STATE_OK, r
    assert r["budget_remaining_pct"] == 100.0, r
    # 2 个坏小时（90/10）→ 预算 (1-0.99)*100 = 1.0，消耗 2 → breached
    _mk_hours(100, done=99, failed=1, bad_hours=2)
    r2 = S.compute(slo, [REPO_DIM], now=NOW)
    assert r2["bad_buckets"] == 2 and abs(r2["budget_total"] - 1.0) < 1e-9, r2
    assert r2["state"] == S.STATE_BREACHED, r2
    assert r2["budget_remaining_pct"] == 0.0, r2


def test_success_rate_warning_at_75pct_budget():
    slo = _slo(S.SLI_SUCCESS)
    # 400 小时、3 个坏小时 → 预算 4.0，消耗 3 → 75% → warning
    _mk_hours(400, done=99, failed=1, bad_hours=3)
    r = S.compute(slo, [REPO_DIM], now=NOW)
    assert r["buckets"] == 400 and r["bad_buckets"] == 3, r
    assert r["state"] == S.STATE_WARNING, r


def test_latency_slo_uses_hourly_p95_threshold():
    slo = _slo(S.SLI_LATENCY)          # 阈值 300s、目标 0.95（95% 的小时要达标）
    for i in range(100):
        v = 900.0 if i == 0 else 100.0        # 1 个超标小时
        MR.write_bucket("hour", _hour(i + 1),
                        {REPO_DIM: {"task_queue_wait_p95": (v, 5)}})
    r = S.compute(slo, [REPO_DIM], now=NOW)
    assert r["buckets"] == 100 and r["bad_buckets"] == 1, r
    assert r["max_p95_sec"] == 300, r
    assert r["window_p95_sec"] == 100.0, r          # 小时 p95 的 95 分位（1/100 超标）
    assert abs(r["budget_total"] - 5.0) < 1e-9 and r["state"] == S.STATE_OK, r
    # 10 个超标小时 → 消耗 10/5 = 200% → breached
    for i in range(10):
        MR.write_bucket("hour", _hour(i + 1),
                        {REPO_DIM: {"task_queue_wait_p95": (900.0, 5)}})
    r2 = S.compute(slo, [REPO_DIM], now=NOW)
    assert r2["bad_buckets"] == 10 and r2["state"] == S.STATE_BREACHED, r2


def test_engine_availability_is_l2_only_and_uses_online_column():
    slo = _slo(S.SLI_AVAIL)
    # 造 4 个小时桶的采样：3 小时在线 60/60、1 小时 30/60
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for h in range(1, 5):
            base = _hour(h)
            total = 60
            online = 30 if h == 1 else 60
            for k in range(total):
                ts = (base + timedelta(seconds=k * 60)).isoformat()
                c.execute("INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at,"
                          " online) VALUES (9901, 'slo-dev', ?, ?)",
                          (ts, 1 if k < online else 0))
        db.commit()
    # L1：不算（引擎可用率属平台目标）
    l1 = S.compute(slo, ["global"], now=NOW, allow_engine=False)
    assert l1["state"] == S.STATE_NO_DATA and l1["reason"] == "l2_only", l1
    # 可用率只从 SLO 建立时刻起算（online 列上线前的历史行是"未知"，不能当离线）→ 把建立时间前移
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE obs_slo SET created_at=? WHERE id=?", (S._iso(NOW - timedelta(days=7)), slo["id"]))
        db.commit()
    slo = S.get_slo(slo["id"]) or slo
    # L2：在线 210/240 = 0.875 < 0.99 → breached
    l2 = S.compute(slo, ["global"], now=NOW, allow_engine=True)
    assert l2["buckets"] == 4, l2
    assert abs(l2["value"] - (210 / 240)) < 1e-3, l2
    assert l2["state"] == S.STATE_BREACHED, l2


def test_empty_scope_never_degrades_to_global():
    """可见仓库为空 → 空序列 → no_data（绝不降级成全量）。"""
    slo = _slo(S.SLI_SUCCESS)
    _mk_hours(10, done=99, failed=1, dim="global")     # 只有 global 有数据
    r = S.compute(slo, [], now=NOW)
    assert r["buckets"] == 0 and r["state"] == S.STATE_NO_DATA, r
    assert r["reason"] == "no_buckets", r
    assert r["value"] is None and r["budget_remaining_pct"] is None, r


# ---------------------------------------------------------------- 快照 / 燃尽

def test_snapshot_upsert_and_cleanup():
    slo = _slo(S.SLI_SUCCESS)
    _mk_hours(10, done=99, failed=1)
    summary = S.compute(slo, [REPO_DIM], now=NOW)
    bucket = _hour(0)
    assert S.write_snapshot(summary, bucket)
    assert S.write_snapshot(summary, bucket)            # 同桶再写 → 覆盖不新增
    snaps = S.snapshots(slo["id"])
    assert len([s for s in snaps if s["bucket_start"] == S._iso(bucket)]) == 1, snaps
    # 清理：留 1 天 → 老快照被删
    S.write_snapshot(summary, NOW - timedelta(days=10))
    assert S.cleanup_snapshots(now=NOW, keep_days=1) >= 1
    assert all(s["bucket_start"] >= S._iso(NOW - timedelta(days=1)) for s in S.snapshots(slo["id"]))


def test_burn_series_cumulative_and_thinned():
    buckets = []
    for i in range(400):
        good = 90.0 if i < 3 else 99.0                 # 前 3 个桶是坏桶
        buckets.append((f"b{i}", good, 100.0))
    series = S.burn_series(buckets, 0.99, NOW - timedelta(hours=400), NOW)
    assert len(series) <= S.MAX_BURN_POINTS + 1, len(series)
    assert series[0]["used_pct"] > 0 and series[0]["bad"] == 1
    assert series[1]["ideal_pct"] > series[0]["ideal_pct"]
    # 消耗单调不减
    used = [p["used_pct"] for p in series]
    assert used == sorted(used), used[:6]


def test_summary_for_strips_burn():
    slo = _slo(S.SLI_SUCCESS)
    _mk_hours(5, done=99, failed=1)
    s = S.summary_for(slo, [REPO_DIM], now=NOW)
    assert "burn" not in s and s["state"] in (S.STATE_OK, S.STATE_WARNING, S.STATE_BREACHED,
                                              S.STATE_NO_DATA)


# ---------------------------------------------------------------- 违约事件 / 周期评估 / 接口

def _events_of_source(source: str):
    from src.observability import events_view as EV
    return EV.unified_events({"id": 1, "username": "admin", "org_id": 0, "org_role": "owner"},
                             sources=[source], limit=50, allow_audit=True,
                             global_scope=True)["events"]


def test_evaluate_all_writes_snapshot_and_fires_then_resolves():
    slo = _slo(S.SLI_SUCCESS)
    # 先造违约：2 个坏小时（预算 1.0 → 消耗 2 → breached）
    # 注意：seed 的 SLO 是 global 范围，`evaluate_all` 按 global 维度算 → 数据也要写 global 维
    _mk_hours(100, done=99, failed=1, bad_hours=2, dim="global")
    r = S.compute(slo, ["global"], now=NOW)
    assert r["state"] == S.STATE_BREACHED, r
    stats = S.evaluate_all(now=NOW)
    assert stats["evaluated"] >= 3 and stats["snapshots"] >= 3, stats
    snaps = S.snapshots(slo["id"])
    assert len(snaps) == 1 and snaps[0]["state"] == S.STATE_BREACHED, snaps
    # 违约事件：source='slo'、rule_id=0、平台级（scope_type=global）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT rule_id, rule_name, level, status, source, dim_key, first_fired_at"
                  " FROM obs_alert_events WHERE rule_name=? ORDER BY id DESC LIMIT 1", (S.SLO_RULE_NAME,))
        row = c.fetchone()
    assert row, "违约未写事件"
    assert row[0] == S.SLO_RULE_ID and row[2] == "platform" and row[3] == "firing" and row[4] == "slo"
    assert row[5] == f"slo:{slo['id']}" and row[6], row        # 首次发生显式写 UTC，不靠 DDL 默认值
    # 违约事件进统一事件视图（L2）
    evs = [e for e in _events_of_source("slo") if e["key"].startswith("alert:")]
    assert evs and evs[0]["kind"] == S.SLO_RULE_NAME, evs
    assert evs[0]["state"] == "firing" and evs[0]["next_step"], evs[0]
    # 修好数据 → 再评估 → 事件 resolved
    _mk_hours(100, done=99, failed=1, dim="global")
    S.evaluate_all(now=NOW)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, resolved_at FROM obs_alert_events WHERE rule_name=?"
                  " ORDER BY id DESC LIMIT 1", (S.SLO_RULE_NAME,))
        row2 = c.fetchone()
    assert row2 and row2[0] == "resolved" and row2[1], row2
    evs2 = [e for e in _events_of_source("slo") if e["kind"] == S.SLO_RULE_NAME]
    assert evs2 and evs2[0]["state"] == "resolved", evs2
    # 恢复后不再重复开新事件（幂等）
    n_before = len([e for e in _events_of_source("slo")])
    S.evaluate_all(now=NOW)
    assert len([e for e in _events_of_source("slo")]) == n_before


def test_slo_routes_l1_and_l2():
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin, require_org_member
    from src.api.main import app

    _mk_hours(20, done=99, failed=1)
    app.dependency_overrides[require_org_member] = lambda: {
        "id": 1, "username": "u1", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            # L1：只出仓库范围能算的两条（含 engine_availability 的排除）
            r = cli.get("/api/obs/slo")
            assert r.status_code == 200, r.text
            names = [s["name"] for s in r.json()["slos"]]
            assert S.SLI_AVAIL not in names, names
            assert S.SLI_SUCCESS in names, names
            assert r.json()["global"] is False
            # L1 拿平台目标 → 404
            avail_id = _slo(S.SLI_AVAIL)["id"]
            assert cli.get(f"/api/obs/slo/{avail_id}").status_code == 404
            # 单条详情带燃尽序列
            sid = _slo(S.SLI_SUCCESS)["id"]
            d = cli.get(f"/api/obs/slo/{sid}")
            assert d.status_code == 200 and "burn" in d.json(), d.text[:200]
            # 该测试用户在测试库里没有可见仓库 → 单位置空（不降级成全量）
            assert d.json()["state"] in (S.STATE_NO_DATA, S.STATE_OK), d.json()
            # L2：三条都在（含引擎可用率）
            r2 = cli.get("/api/admin/obs/slo")
            assert r2.status_code == 200, r2.text
            assert S.SLI_AVAIL in [s["name"] for s in r2.json()["slos"]]
            # L2 改目标 → 生效
            up = cli.post(f"/api/admin/obs/slo/{sid}", json={"target": 0.999})
            assert up.status_code == 200, up.text
            assert abs(float(up.json()["slo"]["target"]) - 0.999) < 1e-9
            bad = cli.post(f"/api/admin/obs/slo/{sid}", json={"target": 2.0})
            assert bad.status_code == 400, bad.text
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        app.dependency_overrides.pop(require_admin, None)
        S.update_slo(_slo(S.SLI_SUCCESS)["id"], target=0.99)


# ---------------------------------------------------------------- 仓库维度（页面选择器）

def _grant(uid: int, repo_key: str):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin,"
                  " is_join) VALUES (?,?,?,?,1,1)", (uid, repo_key, owner, name))
        db.commit()


def test_repo_scope_narrows_to_selected_repo():
    """给了 repo → 只看该仓（与总览页其它 KPI 同口径）；不给 → 可见仓库合计。"""
    from src.observability.overview import RepoNotVisible
    # 注意：repo_dims 走 to_repo_key → 带 gitcode: 前缀，数据维度必须同形
    x_dim, other_dim = "repo:gitcode:org/slo-x", "repo:gitcode:org/slo-y"
    _grant(1, "gitcode:org/slo-x")
    _grant(1, "gitcode:org/slo-y")
    user = {"id": 1, "username": "u1", "org_id": 0, "org_role": "owner"}
    # 本仓全好、另一个仓全坏
    _mk_hours(20, done=100, failed=0, dim=x_dim)
    _mk_hours(20, done=50, failed=50, dim=other_dim)
    slo = _slo(S.SLI_SUCCESS)
    only_x = S.list_summaries(user=user, repo="org/slo-x")[0]
    assert only_x["value"] == 1.0 and only_x["dim_keys"] == [x_dim], only_x
    agg = [r for r in S.list_summaries(user=user) if r["name"] == S.SLI_SUCCESS][0]
    assert 0.5 < agg["value"] < 1.0 and len(agg["dim_keys"]) >= 2, agg
    # 详情同样按仓收窄
    d = S.detail(slo["id"], user=user, repo="org/slo-x")
    assert d and d["value"] == 1.0 and d["burn"], d
    # 越权仓库 → RepoNotVisible（调用方转 404，绝不降级成全量）
    with pytest.raises(RepoNotVisible):
        S.list_summaries(user=user, repo="org/not-mine")


def test_repo_param_route_404_when_not_visible():
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin, require_org_member
    from src.api.main import app

    app.dependency_overrides[require_org_member] = lambda: {
        "id": 9999, "username": "nobody", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            assert cli.get("/api/obs/slo?repo=org/not-mine").status_code == 404
            assert cli.get(f"/api/obs/slo/{_slo(S.SLI_SUCCESS)['id']}?repo=org/not-mine").status_code == 404
            # 管理台不校验可见性：同参数 200（平台视角可看任意仓）
            assert cli.get("/api/admin/obs/slo?repo=org/not-mine").status_code == 200
            assert cli.get("/api/admin/obs/slo").json()["repo"] == ""
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        app.dependency_overrides.pop(require_admin, None)
