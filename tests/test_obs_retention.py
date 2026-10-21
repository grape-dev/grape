"""留存/归档 + 循环自监控 + 告警求值降载的等价性（2026-10-17 审计决定）。

钉死四件事：
1. **事件留存 90 天**：只清「已恢复且过期」的；仍在 firing 的再老也不许删（它是"还在告警"的唯一依据）
2. **台账年度归档**：>365 天搬进 `obs_llm_usage_archive`（不删），近期的留着；
   **时间戳格式混用**（台账 created_at 是数据库本地串，不是 ISO）不会漏搬/误搬；重复跑幂等
3. **循环自监控**：beat/fail 计数、连续失败、恢复后归零；L2 `/api/admin/obs/loops` 要鉴权
4. **告警求值降载的等价性**：改成 3 条 GROUP BY 聚合后，卡住/排队/失败率/突变两窗口的数值
   与"把整表捞进 Python 逐行算"的老口径**完全一致**（这是降载不改变行为的前提）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import alerts as al          # noqa: E402
from src.observability import llm_usage as lu       # noqa: E402
from src.observability import loops as lp           # noqa: E402

UID = 7788
REPO = "gitcode:org/ret-mine"
OTHER = "gitcode:org/ret-theirs"


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_alert_events",
                    "DELETE FROM obs_llm_usage",
                    "DELETE FROM obs_llm_usage_archive",
                    "DELETE FROM agent_tasks WHERE id LIKE 't_ret_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


def _iso(dt):
    return dt.isoformat()


def _task(tid, status, *, created_ago=0, started_ago=None, completed_ago=None, repo=REPO):
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key,"
                  " agent_type, user_id, status, created_at, started_at, completed_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "s-" + tid, 1, "p", repo, "coding", UID, status,
                   _iso(now - timedelta(seconds=created_ago)),
                   _iso(now - timedelta(seconds=started_ago)) if started_ago is not None else None,
                   _iso(now - timedelta(seconds=completed_ago)) if completed_ago is not None else None))
        db.commit()


def _event(status, *, resolved_ago=None, fired_ago=0, rule_id=1, rule_name="task_stuck",
           dim="repo:" + REPO, repo=REPO):
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status,"
                  " source, metric, value, threshold, dim_key, repo_key, first_fired_at,"
                  " last_seen_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rule_id, rule_name, "user", "warning", status, "monitor", rule_name,
                   1.0, 0.0, dim, repo, _iso(now - timedelta(seconds=fired_ago)),
                   _iso(now - timedelta(seconds=fired_ago)),
                   _iso(now - timedelta(seconds=resolved_ago)) if resolved_ago is not None else None))
        db.commit()


def _usage(task_id, created_local, tokens=100):
    """按台账真实写法插入：created_at 是**数据库本地时间串**（datetime('now') 风格）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_llm_usage (dedup_key, task_id, seq, source, model_key,"
                  " input_tokens, output_tokens, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"{task_id}:0", task_id, 0, "hermes", "glm", tokens, 0,
                   created_local, created_local))
        db.commit()


# ---------------------------------------------------------------- 1) 事件留存

def test_event_retention_keeps_firing_and_recent():
    _clean()
    old = 91 * 86400
    _event("resolved", resolved_ago=old)                 # 过期已恢复 → 应删
    _event("resolved", resolved_ago=100)                 # 近期已恢复 → 保留
    _event("firing", fired_ago=old, dim="repo:old-firing")   # 过期但仍 firing → **必须保留**
    assert al.cleanup_events() == 1
    rows = al.list_events_all(limit=50)
    assert len(rows) == 2, [r["status"] for r in rows]
    assert {r["status"] for r in rows} == {"resolved", "firing"}
    # 幂等：再清一次不动
    assert al.cleanup_events() == 0


# ---------------------------------------------------------------- 2) 台账年度归档

def test_usage_archive_moves_old_rows_only():
    _clean()
    now_local = datetime.now()
    old_local = (now_local - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")   # 本地串，>365 天
    new_local = (now_local - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    _usage("t_ret_old", old_local)
    _usage("t_ret_new", new_local)
    r = lu.archive_old_usage()
    assert r["archived"] == 1, r
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT task_id FROM obs_llm_usage"); live = [x[0] for x in c.fetchall()]
        c.execute("SELECT task_id FROM obs_llm_usage_archive"); arc = [x[0] for x in c.fetchall()]
    assert live == ["t_ret_new"], live
    assert arc == ["t_ret_old"], arc
    # 幂等 + 不误搬（再跑一次：没有可搬的）
    assert lu.archive_old_usage()["archived"] == 0


def test_usage_archive_handles_iso_format_too():
    """两种时间戳格式都不能漏搬/误搬（台账默认写本地串，但历史/别的写入方可能写 ISO）。"""
    _clean()
    now = datetime.now(timezone.utc)
    _usage("t_ret_iso_old", (now - timedelta(days=400)).isoformat())
    _usage("t_ret_iso_new", (now - timedelta(days=1)).isoformat())
    r = lu.archive_old_usage()
    assert r["archived"] == 1, r
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT task_id FROM obs_llm_usage_archive")
        assert [x[0] for x in c.fetchall()] == ["t_ret_iso_old"]


# ---------------------------------------------------------------- 3) 循环自监控

def test_loops_counters_and_endpoint():
    lp.reset()
    lp.beat("obs_alert")
    lp.fail("obs_alert", "boom")
    lp.fail("obs_alert", "boom2")
    snap = {r["loop"]: r for r in lp.snapshot()}
    a = snap["obs_alert"]
    assert a["runs"] == 1 and a["failures"] == 2 and a["consecutive_failures"] == 2
    assert a["healthy"] is False and "boom2" in a["last_error"]
    assert a["seconds_since_success"] is not None
    lp.beat("obs_alert")
    a = {r["loop"]: r for r in lp.snapshot()}["obs_alert"]
    assert a["consecutive_failures"] == 0 and a["healthy"] is True
    # L2 端点要鉴权（用真实 app 的**路由与依赖**校验）
    #
    # 注意：这里**不用 `with TestClient(app)`**。用 with 会跑 app 的 lifespan（startup），
    # 那会在同一个 pytest 进程里种规则（把 task_stuck.for_sec 重置为 300）、起后台循环，
    # 污染后续用例（实测：批量跑时 test_obs_alerts 的用例成片失败）。不带 with 就没有 lifespan，
    # 路由/鉴权路径照样是真实的。
    from fastapi.testclient import TestClient
    from src.api.main import app as main_app
    from src.api.deps import require_admin
    cli = TestClient(main_app)
    try:
        assert cli.get("/api/admin/obs/loops").status_code == 401
        main_app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
        r = cli.get("/api/admin/obs/loops")
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(x["loop"] == "obs_alert" for x in body["loops"]), body
        assert "unhealthy" in body
    finally:
        main_app.dependency_overrides.pop(require_admin, None)
        cli.close()


# ---------------------------------------------------------------- 4) 降载等价性

def test_repo_metrics_aggregation_matches_row_by_row():
    """SQL 聚合口径 vs 原逐行口径：卡住/排队/失败率/突变两窗口逐一钉死。"""
    _clean()
    # queued 20 个 → task_queued=20
    for i in range(20):
        _task(f"t_ret_q{i}", "queued", created_ago=60 * (i + 1))
    # running 且 started 早于 22 分钟 → 3 个卡住；1 个刚起跑的 running 不算
    for i in range(3):
        _task(f"t_ret_st{i}", "running", created_ago=3600, started_ago=23 * 60 + i)
    _task("t_ret_fresh", "running", created_ago=60, started_ago=30)
    # 失败率窗口（5 分钟）内：2 completed + 2 failed → rate = 0.5
    _task("t_ret_c1", "completed", created_ago=600, started_ago=590, completed_ago=120)
    _task("t_ret_c2", "completed", created_ago=600, started_ago=590, completed_ago=60)
    _task("t_ret_f1", "failed", created_ago=600, started_ago=590, completed_ago=200)
    _task("t_ret_f2", "failed", created_ago=600, started_ago=590, completed_ago=10)
    # 窗口外（> 5 分钟）的终态不计失败率，但仍在突变窗口 0（<30 分钟）里
    _task("t_ret_old1", "failed", created_ago=3600, started_ago=3500, completed_ago=400)
    # 突变窗口 1（30~60 分钟）：1 ok + 1 bad
    _task("t_ret_m1a", "completed", created_ago=7200, started_ago=7000, completed_ago=2400)
    _task("t_ret_m1b", "failed", created_ago=7200, started_ago=7000, completed_ago=2500)
    # 另一个仓库：不该混进 REPO 的计数
    _task("t_ret_other", "failed", created_ago=600, started_ago=590, completed_ago=30, repo=OTHER)

    m = al._repo_metrics()[REPO]
    assert m["task_queued"] == 20.0, m
    assert m["task_stuck"] == 3.0, m
    # 失败率窗口内：4 个（2 ok + 2 bad）→ 0.5
    assert m["_done"] == 4.0 and m["_bad"] == 2.0, m
    assert m["task_failure_rate"] == 0.5, m
    # 突变窗口0（<30min）：2 ok + 3 bad（含窗口外的 t_ret_old1）= 5 样本
    assert m["_mut0_tot"] == 5.0 and m["_mut0_bad"] == 3.0, m
    # 突变窗口1（30~60min）：1 ok + 1 bad = 2 样本 → 低于 MUTATION_MIN_SAMPLES(3) → 不出突变指标
    assert m["_mut1_tot"] == 2.0 and m["_mut1_bad"] == 1.0, m
    assert "task_success_rate_mutation" not in m, m
    # 另一个仓库独立成维度
    assert al._repo_metrics()[OTHER]["task_failure_rate"] == 1.0


def test_repo_metrics_mutation_emitted_when_enough_samples():
    _clean()
    # 窗口0：3 ok；窗口1：2 ok + 1 bad → r0=1.0, r1=0.6667 → 突变 +0.3333
    for i in range(3):
        _task(f"t_ret_w0_{i}", "completed", created_ago=600, started_ago=590, completed_ago=100 + i)
    for i in range(2):
        _task(f"t_ret_w1_ok{i}", "completed", created_ago=7200, started_ago=7000, completed_ago=2400 + i)
    _task("t_ret_w1_bad", "failed", created_ago=7200, started_ago=7000, completed_ago=2500)
    m = al._repo_metrics()[REPO]
    assert m["_mut0_tot"] == 3.0 and m["_mut1_tot"] == 3.0, m
    # 突变值按 round(...,4) 落库（与实现一致）
    assert m["task_success_rate_mutation"] == round(1.0 - 2.0 / 3.0, 4), m