"""数据缺失（Data Gap）与突变检测测试（P1-3）。

钉死五件事：
1. **设备静默**：心跳落后超过阈值 → 建 gap 事件（`source='gap'`，`first_fired_at`=静默起点）；
   心跳恢复 → resolved
2. **两类不报**：从未上报过（`last_seen_at` 空）不算缺失；超过 7 天视为**已退役凭证**不再报
   （但仍出现在 L2 静默名单里，带 `beyond_retire_window` 标记）
3. **仓库静默**：默认关闭；开启后"近 window 有活动 + 最近 threshold 无活动"才报
4. **突变（环比）**：两个窗口各需 ≥3 个终态任务，样本不足**不出指标**（否则 1~2 个任务的窗口
   会把噪声当突变）；跌幅超阈值触发、恢复后 resolved
5. **可见性**：设备维度事件只给"看得见该设备"的人；越权设备的事件看不见也静默不了
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import alerts as al          # noqa: E402
from src.observability import gap as gmod           # noqa: E402

UID = 7799
REPO = "gitcode:org/gap-mine"
OTHER_REPO = "gitcode:org/gap-theirs"
RT_GLOBAL = "__test_gap_rt_global"
RT_FOREIGN = "__test_gap_rt_foreign"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_alert_events",
                    "DELETE FROM agent_tasks WHERE id LIKE 't_gap_%'",
                    "DELETE FROM runtime_credentials WHERE name LIKE '__test_gap_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        try:
            c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        except Exception:
            pass
        db.commit()


def _set_rule(name, **fields):
    from src.session_manager import db, db_lock
    sets = ", ".join(f"{k}=?" for k in fields)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE obs_alert_rules SET {sets} WHERE name=?", (*fields.values(), name))
        db.commit()


def _rule_id(name):
    return int({r["name"]: r for r in al.list_rules()}[name]["id"])


def _grant(repo_key):
    from src.session_manager import db, db_lock
    owner, _, rname = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin,"
                  " is_join) VALUES (?,?,?,?,1,1)", (UID, repo_key, owner, rname))
        db.commit()


def _mk_runtime(name, scope="global", owner_name="", last_seen=None, status="active",
                org_id=0, repo_key=""):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope,"
                  " owner_name, status, last_seen_at, repo_key, org_id)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  ("hash-" + name, name, name, scope, owner_name, status,
                   _iso(last_seen) if last_seen else None, repo_key, org_id))
        db.commit()
        return int(c.lastrowid)


def _mk_task(tid, repo_key, status, completed_min_ago=None, created_min_ago=None):
    from src.session_manager import db, db_lock
    now = _now()
    ct = _iso(now - timedelta(minutes=completed_min_ago)) if completed_min_ago is not None else None
    crt = _iso(now - timedelta(minutes=(created_min_ago if created_min_ago is not None else 0)))
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key,"
                  " agent_type, user_id, status, created_at, completed_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (tid, "s-" + tid, 1, "p", repo_key, "coding", UID, status, crt, ct))
        db.commit()


def _events(rule_name=None, dim_key=None):
    from src.session_manager import db, db_lock
    sql = ("SELECT e.* FROM obs_alert_events e JOIN obs_alert_rules r ON r.id=e.rule_id WHERE 1=1")
    params = []
    if rule_name:
        sql += " AND r.name=?"
        params.append(rule_name)
    if dim_key:
        sql += " AND e.dim_key=?"
        params.append(dim_key)
    with db_lock:
        c = db.cursor()
        c.execute(sql, tuple(params))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    al.seed_rules()
    _grant(REPO)
    for n in ("runtime_silent", "repo_silent", "task_success_rate_drop",
              "task_stuck", "task_failure_rate", "task_queue_backlog"):
        _set_rule(n, enabled=0)
    yield
    _clean()


# ---------------------------------------------------------------- 设备静默

def test_runtime_gap_fires_and_resolves():
    """心跳落后超过阈值 → gap 事件（first_fired_at = 静默起点）；心跳恢复 → resolved。"""
    rid = _mk_runtime(RT_GLOBAL, scope="global", last_seen=_now() - timedelta(minutes=30))
    _set_rule("runtime_silent", enabled=1, threshold=900)
    stats = gmod.probe_all()
    assert stats["gap_fired"] == 1, stats
    ev = _events(rule_name="runtime_silent", dim_key=f"runtime:{rid}")[0]
    assert ev["status"] == "firing" and ev["source"] == "gap"
    assert ev["metric"] == "gap_runtime" and 1700 < float(ev["value"]) < 1900   # ≈1800s 静默
    silent_since = datetime.fromisoformat(ev["first_fired_at"].replace("Z", "+00:00"))
    # 静默起点 = 最后上报 + 阈值（即"从这一刻起就算该报数却没报"）
    assert abs((silent_since - (_now() - timedelta(minutes=30) + timedelta(seconds=900))
                ).total_seconds()) < 5
    # 心跳恢复 → 事件收口
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE runtime_credentials SET last_seen_at=? WHERE id=?", (_iso(_now()), rid))
        db.commit()
    stats2 = gmod.probe_all()
    assert stats2["gap_resolved"] == 1, stats2
    assert _events(rule_name="runtime_silent")[0]["status"] == "resolved"


def test_never_reported_runtime_is_not_a_gap():
    """从未上报过（last_seen_at 为空）= 没装成/没用过 → 不算缺失。"""
    rid = _mk_runtime(RT_GLOBAL, scope="global", last_seen=None)
    _set_rule("runtime_silent", enabled=1, threshold=900)
    stats = gmod.probe_all()
    assert stats["gap_fired"] == 0, stats
    assert not _events(rule_name="runtime_silent", dim_key=f"runtime:{rid}")


def test_retired_runtime_does_not_fire_but_shows_in_silent_list():
    """超过 7 天没上报 = 已退役凭证：不产生事件（避免永久刷告警），但 L2 名单里能看到。"""
    rid = _mk_runtime(RT_GLOBAL, scope="global", last_seen=_now() - timedelta(days=30))
    _set_rule("runtime_silent", enabled=1, threshold=900)
    stats = gmod.probe_all()
    assert stats["gap_fired"] == 0, stats
    data = gmod.silent_targets()
    row = [x for x in data["runtimes"] if x["id"] == rid][0]
    assert row["beyond_retire_window"] is True and row["silent_sec"] > 7 * 86400
    # include_retired=False 时不再列出
    data2 = gmod.silent_targets(include_retired=False)
    assert not [x for x in data2["runtimes"] if x["id"] == rid]


# ---------------------------------------------------------------- 仓库静默

def test_repo_gap_default_off_then_on():
    """仓库静默：默认关闭（种子 enabled=0）；开启后"近 window 有活动 + 最近 threshold 无活动"才报。"""
    _mk_task("t_gap_a", REPO, "completed", completed_min_ago=90, created_min_ago=120)
    _set_rule("repo_silent", enabled=0, threshold=3600, window_sec=21600)
    assert gmod.probe_all()["gap_fired"] == 0
    assert not _events(rule_name="repo_silent")

    _set_rule("repo_silent", enabled=1, threshold=3600, window_sec=21600)
    stats = gmod.probe_all()
    assert stats["gap_fired"] >= 1, stats
    ev = _events(rule_name="repo_silent", dim_key=f"repo:{REPO}")[0]
    assert ev["source"] == "gap" and ev["metric"] == "gap_repo" and ev["repo_key"] == REPO

    # 有新任务活动 → 收口；且"活动最近但没有超过阈值"不报
    _mk_task("t_gap_b", REPO, "running", created_min_ago=1)
    stats2 = gmod.probe_all()
    assert stats2["gap_resolved"] >= 1, stats2
    assert _events(rule_name="repo_silent", dim_key=f"repo:{REPO}")[0]["status"] == "resolved"


# ---------------------------------------------------------------- 突变（环比）

def test_mutation_metric_requires_min_samples():
    """两个窗口各需 ≥3 个终态任务；样本不足时**不出指标**（该维度本轮跳过）。

    窗口口径：本窗口 = 最近 30 分钟；上一窗口 = 30~60 分钟前。
    """
    for i in range(2):
        _mk_task(f"t_gap_m{i}", REPO, "completed", completed_min_ago=10)
    for i in range(2):
        _mk_task(f"t_gap_m{i+10}", REPO, "failed", completed_min_ago=35 + i)
    m = al._repo_metrics()[REPO]
    assert "task_success_rate_mutation" not in m, "样本不足不该出突变指标"

    for i in range(3):
        _mk_task(f"t_gap_n{i}", REPO, "completed", completed_min_ago=10 + i)
    for i in range(3):
        _mk_task(f"t_gap_n{i+10}", REPO, "failed", completed_min_ago=35 + i)
    m2 = al._repo_metrics()[REPO]
    assert m2["task_success_rate_mutation"] == pytest.approx(1.0 - 0.0, abs=0.01), m2


def test_mutation_rule_fires_on_drop_and_resolves_on_recovery():
    """成功率环比下跌超过阈值 → 触发（source='mutation'）；恢复后 resolved。"""
    _rule_id("task_success_rate_drop")
    _set_rule("task_success_rate_drop", enabled=1, threshold=-0.2, for_sec=0)
    for i in range(4):                      # 本窗口：全失败 → 成功率 0
        _mk_task(f"t_gap_cw{i}", REPO, "failed", completed_min_ago=10 + i)
    for i in range(4):                      # 上一窗口：全成功 → 成功率 1
        _mk_task(f"t_gap_pw{i}", REPO, "completed", completed_min_ago=35 + i)
    r = al.evaluate_rules()
    assert r["metric_fired"] >= 1, r
    ev = _events(rule_name="task_success_rate_drop", dim_key=f"repo:{REPO}")[0]
    assert ev["source"] == "mutation" and ev["status"] == "firing"
    assert float(ev["value"]) == pytest.approx(-1.0, abs=0.01)

    # 本窗口也变成功 → 环比 0（不再满足阈值）→ 收口
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='completed' WHERE id LIKE 't_gap_cw%'")
        db.commit()
    al.evaluate_rules()
    assert _events(rule_name="task_success_rate_drop",
                   dim_key=f"repo:{REPO}")[0]["status"] == "resolved"


def test_evaluate_rules_returns_gap_stats():
    """evaluate_rules 汇总里带 gap 维度（30s 循环日志用它）。"""
    r = al.evaluate_rules()
    for k in ("gap_fired", "gap_resolved", "metric_fired", "metric_resolved", "fired", "resolved"):
        assert k in r, f"缺 {k}"


# ---------------------------------------------------------------- 可见性

def test_runtime_gap_visibility_and_silence():
    """设备维度事件：看得见该设备才看得见它的事件；越权设备看不见也静默不了。"""
    from src.observability.overview import RepoNotVisible      # noqa: F401  (口径同源)
    mine = _mk_runtime(RT_GLOBAL, scope="global", last_seen=_now() - timedelta(minutes=30))
    theirs = _mk_runtime(RT_FOREIGN, scope="user", owner_name="stranger",
                         last_seen=_now() - timedelta(minutes=30))
    _set_rule("runtime_silent", enabled=1, threshold=900)
    gmod.probe_all()
    user = {"id": UID, "username": "gap-tester", "org_id": 0, "org_role": "owner"}
    evs = al.list_events(user, limit=50)
    dims = {e["dim_key"] for e in evs}
    assert f"runtime:{mine}" in dims, dims
    assert f"runtime:{theirs}" not in dims, "他人设备的事件不该可见"

    # 静默：自己的可以，别人的不行（等价 404 语义 → 返回 False）
    my_ev = [e for e in evs if e["dim_key"] == f"runtime:{mine}"][0]
    assert al.silence_event(int(my_ev["id"]), 30, user) is True
    theirs_ev = _events(rule_name="runtime_silent", dim_key=f"runtime:{theirs}")[0]
    assert al.silence_event(int(theirs_ev["id"]), 30, user) is False


def test_platform_level_gap_events_never_reach_user_face():
    """平台级（level='platform'）的缺失事件不进用户面（§5.3）。"""
    from src.session_manager import db, db_lock
    rid = _mk_runtime(RT_GLOBAL, scope="global", last_seen=_now() - timedelta(minutes=30))
    _set_rule("runtime_silent", enabled=1, threshold=900)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE obs_alert_rules SET level='platform' WHERE name='runtime_silent'")
        db.commit()
    try:
        gmod.probe_all()
        user = {"id": UID, "username": "gap-tester", "org_id": 0, "org_role": "owner"}
        dims = {e["dim_key"] for e in al.list_events(user, limit=50)}
        assert f"runtime:{rid}" not in dims, "平台级事件不该出现在用户面"
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE obs_alert_rules SET level='user' WHERE name='runtime_silent'")
            db.commit()

# ---------------------------------------------------------------- L2 接口

def test_admin_gaps_endpoint_and_threshold_policy():
    """管理台 L2：全局静默名单 + 用 threshold 配置"静默多久算缺失"（kind=gap 的阈值语义）。"""
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin
    from src.api.main import app

    fresh = _mk_runtime(RT_GLOBAL, scope="global", last_seen=_now() - timedelta(minutes=5))
    retired = _mk_runtime(RT_FOREIGN, scope="global", last_seen=_now() - timedelta(days=30))
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/admin/obs/gaps")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["global"] is True
            ids = {x["id"]: x for x in body["runtimes"]}
            assert fresh in ids and retired in ids, "全局名单应含所有设备"
            assert ids[retired]["beyond_retire_window"] is True
            assert ids[fresh]["beyond_retire_window"] is False
            r2 = cli.get("/api/admin/obs/gaps", params={"include_retired": "false"})
            assert retired not in {x["id"] for x in r2.json()["runtimes"]}

            # 阈值就是 kind=gap 的"静默多少秒算缺失"，走同一个 policy 写入口
            rid = _rule_id("runtime_silent")
            r3 = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy", json={"threshold": 120})
            assert r3.status_code == 200, r3.text
            assert float(al.get_rule(rid)["threshold"]) == 120.0
            r4 = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy", json={"threshold": "abc"})
            assert r4.status_code == 422, r4.text        # 类型错误：FastAPI 层直接 422
            r5 = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy", json={})
            assert r5.status_code == 400, r5.text        # 空 body：没有可写字段
    finally:
        app.dependency_overrides.pop(require_admin, None)
