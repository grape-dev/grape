"""统一事件视图测试（P1-4 / §6.9）。

钉死四件事：
1. **多来源归一**：告警（monitor/gap/mutation）/ 任务（卡住·失败）/ 变更（PR 合并）都规范成
   同一组字段（含 `started_at`、`duration_sec`、`ongoing`、`next_step`），按时间倒序、
   带 `by_source` 计数
2. **可见性**：用户面只出自己可见仓库/设备的事件；平台级告警与 `audit` 一律不进用户面
3. **L2 全局**：管理台视角全局（含平台级 + `audit`），且与用户面**同一聚合口径**
4. **过滤**：`state`（firing/resolved）、`repo`（越权 → 404）、`limit`
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import alerts as al            # noqa: E402
from src.observability import events_view as ev        # noqa: E402

UID = 7801
OTHER_UID = 7802
REPO = "gitcode:org/ev-mine"
OTHER = "gitcode:org/ev-theirs"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_alert_events",
                    "DELETE FROM audit_logs WHERE event_type='__test_ev_audit'",
                    "DELETE FROM agent_tasks WHERE id LIKE 't_ev_%'",
                    "DELETE FROM events WHERE title LIKE 'ev-test%'",
                    "DELETE FROM runtime_credentials WHERE name LIKE '__test_ev_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        try:
            c.execute("DELETE FROM user_repos WHERE user_id IN (?,?)", (UID, OTHER_UID))
        except Exception:
            pass
        db.commit()


def _grant(uid, repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin,"
                  " is_join) VALUES (?,?,?,?,1,1)", (uid, repo_key, owner, name))
        db.commit()


def _rule(name):
    return {r["name"]: r for r in al.list_rules()}[name]


def _mk_alert(rule_name, repo_key, status="firing", first_min_ago=5, resolved_min_ago=None,
              level=None, source=None, dim_key=None, metric=None, value=1.0):
    from src.session_manager import db, db_lock
    r = _rule(rule_name)
    now = _now()
    first = _iso(now - timedelta(minutes=first_min_ago))
    resolved = _iso(now - timedelta(minutes=resolved_min_ago)) if resolved_min_ago is not None else ""
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status,"
                  " source, metric, value, threshold, dim_key, repo_key, first_fired_at,"
                  " last_seen_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (int(r["id"]), rule_name, level or r["level"], r["severity"], status,
                   source or al._event_source(r), metric or r["metric"], value,
                   float(r["threshold"] or 0), dim_key or f"repo:{repo_key}", repo_key,
                   first, _iso(now), resolved))
        db.commit()
        return int(c.lastrowid)


def _mk_task(tid, repo_key, status, started_min_ago=5, completed_min_ago=None, issue_id="#1"):
    from src.session_manager import db, db_lock
    now = _now()
    st = _iso(now - timedelta(minutes=started_min_ago))
    ct = _iso(now - timedelta(minutes=completed_min_ago)) if completed_min_ago is not None else None
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key,"
                  " agent_type, user_id, status, created_at, started_at, completed_at, issue_id,"
                  " errors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "s-" + tid, 1, "p", repo_key, "coding", UID, status, st, st, ct,
                   issue_id, '["pytest failed: 3 errors"]'))
        db.commit()


def _mk_change(repo_key, issue_id="#1", title="ev-test merge", actor="liufei"):
    from src.session_manager import db, db_lock
    now = _now()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO events (repo_key, event_type, issue_id, title, actor, duration,"
                  " started_at, completed_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (repo_key, "pull_request_merged", issue_id, title, actor, 300.0,
                   _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=25)),
                   _iso(now - timedelta(minutes=30))))
        db.commit()


def _mk_audit(event_type="__test_ev_audit"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COALESCE(MAX(seq),0)+1, COALESCE(MAX(curr_hash),'') FROM audit_logs")
        row = c.fetchone() or (1, "")
        seq, prev = int(row[0]), (row[1] or "")
        c.execute("INSERT INTO audit_logs (seq, event_type, user_id, operator_id, ip, detail,"
                  " prev_hash, curr_hash, created_at)"
                  " VALUES (?,?,0,1,'127.0.0.1','{}',?,?,?)",
                  (seq, event_type, prev, f"hash-{seq}", _iso(_now())))
        db.commit()


USER = {"id": UID, "username": "ev-tester", "org_id": 0, "org_role": "owner"}


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    al.seed_rules()
    _grant(UID, REPO)
    _grant(OTHER_UID, OTHER)
    yield
    _clean()


# ---------------------------------------------------------------- 多来源归一

def test_unified_normalizes_all_sources():
    _mk_alert("task_stuck", REPO, first_min_ago=10)
    _mk_alert("task_failure_rate", REPO, status="resolved", first_min_ago=40,
              resolved_min_ago=30)
    _mk_task("t_ev_stuck", REPO, "running", started_min_ago=30)
    _mk_task("t_ev_failed", REPO, "failed", started_min_ago=20, completed_min_ago=15,
             issue_id="#7")
    _mk_change(REPO, issue_id="#7")
    data = ev.unified_events(USER, limit=50)
    srcs = data["by_source"]
    assert srcs.get("monitor") == 2, srcs
    assert srcs.get("task") == 2, srcs
    assert srcs.get("change") == 1, srcs
    assert data["total"] == 5, data["total"]

    # 时间倒序
    starts = [x["started_at"] for x in data["events"]]
    assert starts == sorted(starts, reverse=True), starts

    by_key = {x["key"]: x for x in data["events"]}
    # 告警：首次发生 + 已持续（firing → ongoing）
    a = [x for x in data["events"] if x["source"] == "monitor" and x["state"] == "firing"][0]
    assert a["ongoing"] is True and 590 <= a["duration_sec"] <= 610, a
    assert a["next_step"] == ["logs"] and a["repo_key"] == REPO
    # S1 契约：告警 = 区间事件、无 task 链接 → 不给 timeline、退化到 logs；级别与结果徽标分列
    assert a["ts_kind"] == "interval" and a["outcome"] == "" and a["state_label_key"] == "observability.state.firing"
    assert a["target"] == {"repo_key": REPO, "issue_id": "", "device_id": ""}, a["target"]
    # resolved → 不再 ongoing，时长 = resolved - first（10 分钟）
    r = [x for x in data["events"] if x["state"] == "resolved"][0]
    assert r["ongoing"] is False and 590 <= r["duration_sec"] <= 610, r
    # 任务：卡住带 task_id 与 issue，失败带诊断
    stuck = [x for x in data["events"]
             if x["source"] == "task" and x["kind"] == "task_stuck"][0]
    failed = [x for x in data["events"] if x["source"] == "task" and x["kind"] == "task_failed"][0]
    assert stuck["task_id"] == "t_ev_stuck" and stuck["state"] == "stuck"
    assert failed["issue_id"] == "#7" and "pytest failed" in (failed.get("detail") or "")
    assert failed["duration_sec"] == 300, failed
    # 变更：瞬时/已完成，带 issue 与标题
    ch = [x for x in data["events"] if x["source"] == "change"][0]
    assert ch["state"] == "done" and ch["issue_id"] == "#7" and ch["next_step"] == ["issue"]
    # S1 契约：变更是瞬时事件（point）+ 结果码 merged；target 结构化带 issue
    assert ch["ts_kind"] == "point" and ch["outcome"] == "merged"
    assert ch["target"]["issue_id"] == "#7" and ch["target"]["device_id"] == ""
    # 任务：结果码与告警级别分列
    assert failed["outcome"] == "failed" and stuck["outcome"] == "stuck"
    assert failed["ts_kind"] == "interval" and failed["ended_at"]
    assert by_key  # 占位：确保 by_key 可用


def _mk_runtime(name, scope="global", last_seen=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope,"
                  " owner_name, status, last_seen_at, repo_key, org_id)"
                  " VALUES (?,?,?,?,%s,?,?,?,0)" % "''",
                  ("h-" + name, name, name, scope,
                   "active", _iso(last_seen) if last_seen else None, ""))
        db.commit()
        return int(c.lastrowid)


def test_gap_and_mutation_sources_are_labelled():
    dev = _mk_runtime("__test_ev_dev", scope="global", last_seen=_now() - timedelta(minutes=30))
    _mk_alert("runtime_silent", "", source="gap", metric="gap_runtime",
              dim_key=f"runtime:{dev}", value=1800.0)
    _mk_alert("task_success_rate_drop", REPO, source="mutation", metric="task_success_rate_mutation",
              value=-0.4)
    data = ev.unified_events(USER, limit=20)
    srcs = data["by_source"]
    assert srcs.get("gap") == 1 and srcs.get("mutation") == 1, srcs
    gap = [x for x in data["events"] if x["source"] == "gap"][0]
    assert gap["next_step"] == ["device", "logs"] and gap["dim_key"] == f"runtime:{dev}"
    # S1 契约：设备维度进结构化 target（前端不用再拆 dim_key 字符串）
    assert gap["target"]["device_id"] == str(dev), gap["target"]
    assert gap["state_label_key"] == "observability.state.firing"


# ---------------------------------------------------------------- 可见性

def test_user_face_excludes_other_repos_and_platform_and_audit():
    _mk_alert("task_stuck", REPO)
    _mk_alert("task_stuck", OTHER)                     # 他人仓库
    _mk_alert("db_pool_usage", "", level="platform")    # 平台级
    _mk_task("t_ev_other", OTHER, "failed", completed_min_ago=1)
    _mk_change(OTHER, title="ev-test theirs")
    _mk_audit()
    data = ev.unified_events(USER, limit=50)
    repos = {x["repo_key"] for x in data["events"]}
    assert OTHER not in repos, "他人仓库的事件不该出现"
    assert "audit" not in data["by_source"], "审计来源只在管理台"
    assert all(x["source"] != "audit" for x in data["events"])
    assert not [x for x in data["events"] if x["level"] == "platform"], "平台级告警不进用户面"
    assert data["by_source"].get("monitor") == 1, data["by_source"]


def test_global_scope_sees_everything_and_audit():
    _mk_alert("task_stuck", OTHER)
    _mk_alert("db_pool_usage", "", level="platform")
    _mk_audit()
    data = ev.unified_events({"id": 0, "username": "", "org_id": 0}, limit=50,
                             allow_audit=True, global_scope=True)
    srcs = data["by_source"]
    assert srcs.get("audit", 0) >= 1, srcs
    assert data["global"] is True
    # 平台级与管理台来源都在
    assert any(x.get("scope") == "platform" for x in data["events"]), "L2 应能看到平台级事件"
    assert any(x["source"] == "audit" for x in data["events"])


# ---------------------------------------------------------------- 过滤

def test_state_repo_and_limit_filters():
    _mk_alert("task_stuck", REPO, status="firing")
    _mk_alert("task_failure_rate", REPO, status="resolved", first_min_ago=30,
              resolved_min_ago=20)
    _mk_alert("task_queue_backlog", OTHER, status="firing")
    only_firing = ev.unified_events(USER, state="firing", limit=20)
    assert all(x["state"] in ("firing", "stuck") for x in only_firing["events"])
    only_resolved = ev.unified_events(USER, state="resolved", limit=20)
    assert only_resolved["by_source"].get("monitor") == 1
    # repo 过滤
    one = ev.unified_events(USER, repo="org/ev-mine", limit=20)
    assert all(x["repo_key"] == REPO for x in one["events"] if x["repo_key"])
    # limit
    for i in range(5):
        _mk_alert("task_stuck", REPO, dim_key=f"repo:{REPO}#{i}")
    lim = ev.unified_events(USER, limit=3)
    assert len(lim["events"]) == 3 and lim["total"] == 3, "total 是本次返回条数（各来源先各取 limit 再合并截断）"


def test_repo_out_of_scope_raises():
    from src.observability.overview import RepoNotVisible
    with pytest.raises(RepoNotVisible):
        ev.unified_events(USER, repo="org/ev-theirs", limit=10)


# ---------------------------------------------------------------- 路由层

def test_l1_and_l2_routes():
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin, require_org_member
    from src.api.main import app

    _mk_alert("task_stuck", REPO)
    _mk_audit()
    app.dependency_overrides[require_org_member] = lambda: USER
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/events")
            assert r.status_code == 200, r.text
            b = r.json()
            assert b["events"] and b["by_source"].get("monitor", 0) >= 1
            assert all(x["source"] != "audit" for x in b["events"])
            # 越权仓库 → 404
            assert cli.get("/api/obs/events", params={"repo": "org/ev-theirs"}).status_code == 404
            # 关键字检索（后端）：命中规则名 → 只回 monitor；未命中 → 空
            r4 = cli.get("/api/obs/events", params={"q": "task_stuck"})
            assert r4.status_code == 200, r4.text
            assert r4.json()["q"] == "task_stuck"
            assert r4.json()["by_source"].get("monitor", 0) >= 1, r4.json()["by_source"]
            assert cli.get("/api/obs/events", params={"q": "zzz-nope"}).json()["events"] == []
            # LIKE 元字符（历史地雷：字面 % + 参数 → pymysql 抛错 → 事件静默消失）
            r5 = cli.get("/api/admin/obs/events", params={"q": "%"})
            assert r5.status_code == 200, r5.text
            # 来源过滤
            r2 = cli.get("/api/obs/events", params={"sources": "change"})
            assert r2.status_code == 200 and r2.json()["by_source"].get("monitor") is None
            # L2：全局 + audit
            r3 = cli.get("/api/admin/obs/events")
            assert r3.status_code == 200, r3.text
            assert r3.json()["global"] is True
            assert r3.json()["by_source"].get("audit", 0) >= 1, r3.json()["by_source"]
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        app.dependency_overrides.pop(require_admin, None)


# ------------------------------------------------- 关键字检索（2026-10-18 拍板：后端检索）

def test_q_searches_the_whole_source_not_just_the_loaded_page():
    """`q` 由服务端检索各来源：告警 → rule_name/dim_key；变更 → title/actor/issue_id；
    任务 → 服务端对该来源的当前卡住/失败列表过滤（它本身是 ops_overview 的 Top-N 视图）。"""
    _mk_alert("task_stuck", REPO, first_min_ago=10)
    _mk_task("t_ev_stuck", REPO, "running", started_min_ago=30)
    _mk_change(REPO, issue_id="#77", title="ev-test merge keyword-zzz", actor="liufei")

    only_change = ev.unified_events(USER, limit=50, q="keyword-zzz")
    assert [x["source"] for x in only_change["events"]] == ["change"], only_change["events"]
    assert only_change["q"] == "keyword-zzz"

    only_task = ev.unified_events(USER, limit=50, q="T_EV_STUCK")          # 大小写不敏感
    assert [x["source"] for x in only_task["events"]] == ["task"], only_task["events"]

    has_alert = ev.unified_events(USER, limit=50, q="task_stuck")
    assert "monitor" in [x["source"] for x in has_alert["events"]], has_alert["events"]

    assert ev.unified_events(USER, limit=50, q="no-such-keyword-xyz")["events"] == []


def test_q_metacharacters_never_break_sql():
    """LIKE 元字符/引号/超长输入都不得把 SQL 拼坏 —— 关键字永远只走参数绑定。

    回归背景：SQL 文本里出现字面 `%` 又同时带参数时 pymysql 当格式符抛异常，调用处的 try
    会吞掉异常 → 用户面事件整类**静默消失**（真机踩过）。这里逐个验，且要求**仍有结果**。
    """
    _mk_alert("task_stuck", REPO, first_min_ago=10)
    _mk_change(REPO, title="ev-test merge", actor="liufei")
    for q in ("%", "_", "100%", "a_b", "%' OR '1'='1", "  ev-test  "):
        data = ev.unified_events(USER, limit=50, q=q)
        assert isinstance(data["events"], list), q
        assert data["q"] == q.strip()[:64], (q, data["q"])
    # `%`/`_` 按通配符处理（与日志检索 logs.search 一致）→ 命中全部；超长截断到 64
    assert len(ev.unified_events(USER, limit=50, q="%")["events"]) >= 2
    assert ev.unified_events(USER, limit=50, q="x" * 200)["q"] == "x" * 64


def test_q_audit_source_is_l2_only():
    """审计来源的检索只在 L2 生效（用户面 want 里根本没有 audit）。"""
    _mk_audit()
    assert ev.unified_events(USER, limit=50, q="__test_ev_audit")["events"] == []
    data = ev.unified_events(USER, sources=["audit"], limit=50, allow_audit=True,
                             global_scope=True, q="__test_ev_audit")
    assert [x["source"] for x in data["events"]] == ["audit"], data["events"]


# ------------------------------------------------- S1 契约：state 全集 / 游标分页 / 截断标记

def test_state_full_set_filter():
    """`state` 全集：failed/stuck/done 直取；旧值 firing/resolved 保持兼容（含 stuck/failed 别名）。"""
    _mk_alert("task_stuck", REPO, first_min_ago=10)
    _mk_alert("task_failure_rate", REPO, status="resolved", first_min_ago=40, resolved_min_ago=30)
    _mk_task("t_ev_stuck", REPO, "running", started_min_ago=30)
    _mk_task("t_ev_failed", REPO, "failed", started_min_ago=20, completed_min_ago=15, issue_id="#7")
    _mk_change(REPO, issue_id="#9")

    def srcs(st):
        return sorted({x["source"] for x in ev.unified_events(USER, limit=50, state=st)["events"]})

    assert srcs("failed") == ["task"]
    assert srcs("stuck") == ["task"]
    assert srcs("done") == ["change"]
    assert srcs("firing") == ["monitor", "task"]      # 旧别名：firing 含 stuck
    assert srcs("resolved") == ["monitor", "task"]    # 旧别名：resolved 含 failed
    assert srcs("cancelled") == []
    assert srcs("") == ["change", "monitor", "task"]
    # `state` 原样回显（前端据此高亮）
    assert ev.unified_events(USER, limit=50, state="failed")["state"] == "failed"


def test_cursor_pagination_and_truncated_sources():
    """游标分页：`before=next_cursor` 取下一页（不重不漏）；末页 `next_cursor=None`；
    来源取满 limit → `truncated_sources` 报出来（该来源窗口可能还有更早的事件）。"""
    # 7 条 > 取数窗口(3×limit=6) → 既要有下一页，也要报截断
    for i in range(7):
        _mk_change(REPO, issue_id=f"#{80 + i}", title=f"ev-test merge {i}")

    p1 = ev.unified_events(USER, limit=2)
    assert p1["total"] == 2 and p1["next_cursor"], p1
    assert p1["truncated_sources"] == ["change"], p1["truncated_sources"]
    k1 = [x["key"] for x in p1["events"]]

    p2 = ev.unified_events(USER, limit=2, before=p1["next_cursor"])
    k2 = [x["key"] for x in p2["events"]]
    assert k2 and not (set(k1) & set(k2)), (k1, k2)

    end = ev.unified_events(USER, limit=2, before="2000-01-01T00:00:00+00:00|zzz")
    assert end["events"] == [] and end["next_cursor"] is None
    # 非法游标当首页处理（不抛错）
    bad = ev.unified_events(USER, limit=2, before="not-a-cursor")
    assert bad["total"] == 2, bad
    assert ev.parse_cursor("") is None and ev.parse_cursor("a|b") == ("a", "b")


def test_s1_contract_keys_present_on_every_source():
    """S1 契约字段在四类来源上都存在（前端只依赖这些键，不再猜字段）。"""
    _mk_alert("task_stuck", REPO, first_min_ago=10)
    _mk_task("t_ev_stuck", REPO, "running", started_min_ago=30)
    _mk_change(REPO, issue_id="#7")
    _mk_audit()
    data = ev.unified_events(USER, limit=50, sources=["monitor", "task", "change"], allow_audit=False)
    need = {"ts_kind", "outcome", "state_label_key", "ended_at", "target", "next_step"}
    for it in data["events"]:
        missing = need - set(it)
        assert not missing, (it["source"], missing)
        assert it["ts_kind"] in ("interval", "point")
        assert isinstance(it["next_step"], list) and it["next_step"], it
        assert set(it["target"]) == {"repo_key", "issue_id", "device_id"}
    # 审计（L2）：瞬时事件且无下钻动作
    au = ev.unified_events(USER, sources=["audit"], limit=10, allow_audit=True,
                           global_scope=True)["events"]
    assert au and au[0]["ts_kind"] == "point" and au[0]["next_step"] == []
