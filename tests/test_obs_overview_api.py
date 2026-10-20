"""可观测性总览 API 测试：聚合正确性 + 可见性边界（/api/obs/overview）。

设计：docs/weekly-07/observability-overview-page-design.md §6
      docs/weekly-07/observability-redesign-design.md §5.3（可见性边界）

钉死三件容易回归的事：
1. **越权仓库 404**（不是 403 —— 不区分"不存在"与"无权限"，避免探测存在性）
2. **可见范围为空时返回空聚合，绝不降级为"全部可见"**（否则等于没有边界）
3. **只返回用户自己的资产**：他人仓库的任务/用量不得出现在结果里
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import overview as ov  # noqa: E402


# ------------------------------------------------------------------ 夹具

def _reset(uid=4242):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in (
            "DELETE FROM user_repos WHERE user_id=?",
            "DELETE FROM agent_tasks WHERE id LIKE 't_obsov_%'",
            "DELETE FROM obs_llm_usage WHERE task_id LIKE 't_obsov_%'",
        ):
            try:
                c.execute(sql, (uid,)) if "user_id" in sql else c.execute(sql)
            except Exception:
                pass
        db.commit()


def _grant_repo(uid, repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join)"
                  " VALUES (?,?,?,?,1,1)", (uid, repo_key, owner, name))
        db.commit()


def _make_task(uid, tid, repo_key, status, created, completed, started=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, agent_type,"
            " user_id, status, created_at, started_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "s-" + tid, 1, "p", repo_key, "coding", uid, status,
             created, started, completed))
        db.commit()


def _make_usage(tid, repo_key, model, i, o, cr=0, cw=0, rz=0, created=None):
    from src.session_manager import db, db_lock
    created = created or _iso(datetime.now(timezone.utc))
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO obs_llm_usage (dedup_key, task_id, seq, turn, source, repo_key, model_key,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{tid}:1", tid, 1, 0, "hermes", repo_key, model, i, o, cr, cw, rz, created, created))
        db.commit()


def _iso(dt):
    return dt.isoformat()


# ------------------------------------------------------------------ 可见性边界

def test_out_of_scope_repo_is_404():
    """越权仓库 → 404（不给 403，避免探测存在性）。"""
    uid = 4242
    _reset(uid)
    _grant_repo(uid, "gitcode:org/mine")
    user = {"id": uid, "username": "u4242", "org_id": 0}
    with pytest.raises(ov.RepoNotVisible):
        ov.build_overview(user, repo="org/someone-elses-repo", range_str="24h")
    # 自己可见的仓库正常返回
    got = ov.build_overview(user, repo="org/mine", range_str="24h")
    assert got["scope"]["repos"] == 1


def test_empty_scope_returns_empty_not_everything():
    """可见仓库为空 → 空聚合；不能因为"查不到仓库"就退化成全量。"""
    user = {"id": 987654, "username": "nobody", "org_id": 0}
    got = ov.build_overview(user, repo="", range_str="24h")
    assert got["scope"]["repos"] == 0
    assert got["kpis"]["queue_depth"] == 0
    assert got["charts"]["model_share"] == []
    assert sum(got["charts"]["task_outcome"]["completed"]) == 0


def test_other_users_tasks_are_not_visible():
    """他人仓库的任务与用量不得出现在结果里（可见范围只含自己的仓库）。"""
    uid = 4242
    _reset(uid)
    _grant_repo(uid, "gitcode:org/mine")
    user = {"id": uid, "username": "u4242", "org_id": 0}
    now = datetime.now(timezone.utc)
    _make_task(uid, "t_obsov_mine", "gitcode:org/mine", "completed",
               _iso(now - timedelta(minutes=10)), _iso(now - timedelta(minutes=9)),
               _iso(now - timedelta(minutes=9, seconds=30)))
    _make_task(uid, "t_obsov_theirs", "gitcode:org/theirs", "failed",
               _iso(now - timedelta(minutes=10)), _iso(now - timedelta(minutes=5)),
               _iso(now - timedelta(minutes=9)))
    _make_usage("t_obsov_mine", "gitcode:org/mine", "glm-5.3", 1000, 100,)
    _make_usage("t_obsov_theirs", "gitcode:org/theirs", "glm-5.3", 999999, 999999)

    got = ov.build_overview(user, repo="", range_str="24h")
    to = got["charts"]["task_outcome"]
    assert sum(to["completed"]) == 1, "应只看到自己仓库的 1 个完成任务"
    assert sum(to["failed"]) == 0, "他人仓库的失败任务不得出现"
    assert got["kpis"]["tokens"] == 1100, f"用量应只含自己仓库: {got['kpis']['tokens']}"
    _reset(uid)


# ------------------------------------------------------------------ 聚合正确性

def test_kpis_and_charts_are_computed():
    uid = 4242
    _reset(uid)
    _grant_repo(uid, "gitcode:org/mine")
    user = {"id": uid, "username": "u4242", "org_id": 0}
    now = datetime.now(timezone.utc)
    # 2 完成 + 1 失败 → 成功率 66.7；1 个排队中 → 队列深度 1
    for i, (st, dur) in enumerate([("completed", 10), ("completed", 20), ("failed", 30)]):
        _make_task(uid, f"t_obsov_{i}", "gitcode:org/mine", st,
                   _iso(now - timedelta(hours=2)), _iso(now - timedelta(minutes=5 * i + 1)),
                   _iso(now - timedelta(minutes=5 * i + 1 + dur)))
    _make_task(uid, "t_obsov_q", "gitcode:org/mine", "queued", _iso(now), None, None)
    _make_usage("t_obsov_0", "gitcode:org/mine", "glm-5.3", 1000, 100, cr=500, rz=20)
    _make_usage("t_obsov_1", "gitcode:org/mine", "deepseek-v4-flash-0731", 300, 30)

    got = ov.build_overview(user, repo="", range_str="24h")
    k = got["kpis"]
    assert k["queue_depth"] == 1, k
    assert k["success_rate"] == pytest.approx(66.7, abs=0.1), k
    assert k["p95_duration_s"] is not None and k["p95_duration_s"] >= 10
    # token 总量 = 1000+100+500+20 + 300+30
    assert k["tokens"] == 1950, k
    ms = got["charts"]["model_share"]
    assert [m["model"] for m in ms] == ["glm-5.3", "deepseek-v4-flash-0731"], ms
    assert ms[0]["tokens"] == 1620
    assert round(sum(m["share"] for m in ms), 1) == 100.0
    tt = got["charts"]["token_trend"]
    assert sum(tt["input"]) == 1300 and sum(tt["cache"]) == 500
    _reset(uid)


def test_response_has_no_platform_infrastructure_fields():
    """面向用户的响应不得带平台基础设施信息（DB/连接池/主机拓扑）。"""
    uid = 4242
    _reset(uid)
    _grant_repo(uid, "gitcode:org/mine")
    got = ov.build_overview({"id": uid, "username": "u4242", "org_id": 0}, range_str="24h")
    blob = json.dumps(got, ensure_ascii=False).lower()
    for banned in ("threads_connected", "aborted_connects", "max_used_connections",
                   "pool", "mysql", "pool_size", "database"):
        assert banned not in blob, f"响应里出现了平台内部信息: {banned}"
    _reset(uid)


# ------------------------------------------------------------------ 路由层

def test_route_maps_out_of_scope_to_404_and_returns_json():
    """路由层：越权 → 404；正常 → 200 且可 JSON 序列化（Decimal 等已在服务层拍平）。"""
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    uid = 4242
    _reset(uid)
    _grant_repo(uid, "gitcode:org/mine")
    user = {"id": uid, "username": "u4242", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/overview", params={"repo": "org/others", "range": "24h"})
            assert r.status_code == 404, r.text
            r = cli.get("/api/obs/overview", params={"range": "24h"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body) >= {"range", "bucket_seconds", "kpis", "charts", "scope"}
            assert body["range"] == "24h"
            # 非法 range 回落默认值，不报错
            r = cli.get("/api/obs/overview", params={"range": "99y"})
            assert r.status_code == 200 and r.json()["range"] == ov.DEFAULT_RANGE
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        _reset(uid)