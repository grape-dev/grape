"""能效看板 2.0（flow efficiency）P1 聚合测试。

钉死六条容易回归的口径（对应 docs/weekly-10/efficiency-board-redesign-2026-11-09.md）：

1. **周边界**：ISO 周、周一起算、CST——周日 23:59 与周一 00:00 必须落在不同桶。
2. **累计积压回算**：周末积压 = created<周末 − completed<周末，不依赖历史快照表；
   最新一周的积压必须等于当前未关闭数（与 ③ 同源，两处能交叉验证）。
3. **超龄口径**：50 天 < 阈值，本周未关闭的全部按龄期计数；阈值是"龄期"不是"期限"
   （grape 无 due date，payload 里 due_date 实测全空且未落库）。
4. **周期时间样本**：只认 completed 且有 completed_at；completed_at < created_at 的脏数据
   进 dirty_skipped 而不是负数分位数；全部 <1 天时 unit 自动切 hours（"P50 0.0 天"没有信息量）。
5. **子任务不计**：is_subtask_mode=1 的行不进任何口径（归属父需求，防重复计）。
6. **端点契约**：注册在 /api/dashboard/efficiency/board、仓库隔离、空 repo 空态、
   匿名 401。
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import efficiency as eff  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, 0)   # 2026-11-09 是周一，ISO 2026-W46


def _cursor():
    """内存 SQLite + 最小 issue 表（只建本模块用到的列）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE issue (status TEXT, created_at TEXT, completed_at TEXT,"
        " created_by TEXT, assignee TEXT, user_id INTEGER, repo_key TEXT,"
        " is_subtask_mode INTEGER)")
    return conn, conn.cursor()


def _seed(c, rows, repo_key="gitcode:org/repo"):
    for r in rows:
        c.execute(
            "INSERT INTO issue (status, created_at, completed_at, created_by,"
            " assignee, user_id, repo_key, is_subtask_mode)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (r.get("status", "processing"), r["created_at"], r.get("completed_at", ""),
             r.get("created_by", ""), r.get("assignee", ""), r.get("user_id", 0),
             repo_key, r.get("is_subtask", 0)))
    c.connection.commit()


# ------------------------------------------------------------- 周边界

def test_week_boundary_monday_start():
    """周日 23:59 与周一 00:00 分属两周；2026-11-09 周一属 W46。"""
    assert eff.iso_week_label(NOW) == "2026-W46"
    sun = datetime(2026, 11, 8, 23, 59, 0)
    mon = datetime(2026, 11, 9, 0, 0, 0)
    assert eff.week_start(sun).date() == datetime(2026, 11, 2).date()
    assert eff.week_start(mon).date() == datetime(2026, 11, 9).date()
    assert eff.iso_week_label(sun) == "2026-W45"
    assert eff.iso_week_label(mon) == "2026-W46"


def test_created_split_across_weeks():
    conn, c = _cursor()
    _seed(c, [
        {"created_at": "2026-11-08 23:59:00"},   # W45 周日
        {"created_at": "2026-11-09 00:00:00"},   # W46 周一
        {"created_at": "2026-11-09 08:00:00"},   # W46
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    tp = eff.build_throughput(rows, NOW, weeks=2)
    assert tp["weeks"] == ["W45", "W46"]
    assert tp["created"] == [1, 2], tp


# ------------------------------------------------------------- 累计积压

def test_backlog_recalc_and_matches_open_total():
    """积压回算：第 N 周末积压 = 之前创建 − 之前完成；最新一周 = 当前未关闭数。"""
    conn, c = _cursor()
    _seed(c, [
        # 上周创建未完成
        {"created_at": "2026-11-03 10:00:00", "status": "processing"},
        # 上周创建、本周一完成
        {"created_at": "2026-11-03 10:00:00", "status": "completed",
         "completed_at": "2026-11-09 09:00:00"},
        # 本周创建已取消（不算完成，也不算在制）
        {"created_at": "2026-11-09 08:00:00", "status": "cancelled",
         "completed_at": "2026-11-09 09:30:00"},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    tp = eff.build_throughput(rows, NOW, weeks=2)
    summary = eff.build_summary(rows, NOW)
    # W45 末：创建 2，完成 0 → 积压 2；W46 末：创建 3，完成 1 → 积压 2（cancelled 不进 completed）
    assert tp["backlog"] == [2, 2], tp
    assert tp["created"] == [2, 1], tp
    assert tp["completed"] == [0, 1], tp
    assert summary["open_total"] == 1, summary   # 只有 processing 那条算在制
    assert tp["backlog"][-1] == summary["open_total"] + 1  # +1 = cancelled 也算"关闭"


def test_net_flow_week_math_and_trend():
    conn, c = _cursor()
    rows = [
        {"created_at": "2026-11-09 08:00:00", "status": "processing"},
        {"created_at": "2026-11-09 09:00:00", "status": "processing"},
        {"created_at": "2026-11-04 08:00:00", "status": "completed",
         "completed_at": "2026-11-09 10:00:00"},
    ]
    _seed(c, rows)
    fetched = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    nf = eff.build_net_flow(fetched, NOW)
    assert nf["week"] == "2026-W46"
    assert nf["week_created"] == 2
    assert nf["week_completed"] == 1
    assert nf["week_net"] == 1
    assert nf["backlog_trend"] == "up", nf   # 积压从 1 涨到 2


# ------------------------------------------------------------- 超龄

def test_aging_threshold_7_days_is_by_age_not_due_date():
    conn, c = _cursor()
    _seed(c, [
        {"created_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")},
        {"created_at": (NOW - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")},
        {"created_at": (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")},
        # 已关闭的不进龄期
        {"created_at": (NOW - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"),
         "status": "completed",
         "completed_at": (NOW - timedelta(days=38)).strftime("%Y-%m-%d %H:%M:%S")},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    ag = eff.build_aging(rows, NOW, threshold_days=7)
    assert ag["open"] == 3
    assert ag["threshold_days"] == 7
    assert ag["over_threshold"] == 2, ag       # 8 天 + 30 天
    assert ag["sample"] == 3
    assert sum(b["count"] for b in ag["buckets"]) == 3
    # 桶边界：1 天 → "8-24小时"；8 天 → "1-2周"；30 天 → ">2周"
    by_label = {b["label"]: b["count"] for b in ag["buckets"]}
    assert by_label["8-24小时"] == 1
    assert by_label["1-2周"] == 1
    assert by_label[">2周"] == 1


# ------------------------------------------------------------- 周期时间

def test_cycle_time_skips_dirty_and_switches_to_hours():
    conn, c = _cursor()
    _seed(c, [
        # 2 分钟 / 4 分钟：分钟级 → unit=hours
        {"created_at": "2026-11-09 09:00:00", "status": "completed",
         "completed_at": "2026-11-09 09:02:00"},
        {"created_at": "2026-11-09 09:00:00", "status": "completed",
         "completed_at": "2026-11-09 09:04:00"},
        # 脏数据：完成早于创建 → 只进 dirty_skipped
        {"created_at": "2026-11-09 09:00:00", "status": "completed",
         "completed_at": "2026-11-09 08:00:00"},
        # 未完成不进样本
        {"created_at": "2026-11-09 09:00:00", "status": "processing"},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    ct = eff.build_cycle_time(rows)
    assert ct["sample"] == 2, ct
    assert ct["dirty_skipped"] == 1, ct
    assert ct["unit"] == "minutes", ct   # P90 < 1 小时 → 分钟级出数
    assert ct["p50"] == 2.0, ct          # [2,4] 分钟的 nearest-rank 中位取到 2
    assert sum(b["count"] for b in ct["buckets"]) == 2


# ------------------------------------------------------------- 子任务 / 空态

def test_subtask_rows_excluded():
    conn, c = _cursor()
    _seed(c, [
        {"created_at": "2026-11-09 08:00:00"},
        {"created_at": "2026-11-09 09:00:00", "is_subtask": 1},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue WHERE is_subtask_mode=0").fetchall()
    out = eff.build_board(c, ["gitcode:org/repo"], repo_label="org/repo", now=NOW)
    assert out["summary"]["created_total"] == 1, out


def test_empty_repo_keys_returns_empty_state():
    out = eff.build_board(None, [], repo_label="", now=NOW)
    assert out["empty"] is True
    assert out["message"] == "no_repo_selected"
    assert out["summary"] == {}


def test_no_rows_sets_empty_flag():
    conn, c = _cursor()
    out = eff.build_board(c, ["gitcode:org/repo"], repo_label="org/repo", now=NOW)
    assert out["empty"] is True
    assert out["summary"]["created_total"] == 0
    assert out["throughput"]["created"] == [0] * 12
    assert out["cycle_time"]["sample"] == 0
    assert out["cycle_time"]["p50"] is None


# ------------------------------------------------------------- 端点契约

def _client():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    user = {"id": 122902, "username": "flow-board-user", "org_id": 0,
            "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    return TestClient(app), app, require_org_member


def test_endpoint_registered_and_empty_repo_state():
    cli, app, dep = _client()
    try:
        r = cli.get("/api/dashboard/efficiency/board")
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        # 未指定仓库 → 空态，不做全库聚合
        assert body.get("empty") is True
        assert body.get("message") == "no_repo_selected"
    finally:
        app.dependency_overrides.pop(dep, None)


def test_endpoint_requires_auth():
    from fastapi.testclient import TestClient
    from src.api.main import app
    cli = TestClient(app)
    r = cli.get("/api/dashboard/efficiency/board", params={"repo": "org/repo"})
    assert r.status_code in (401, 403), r.status_code

# ------------------------------------------------------------- ⑨ 在办分布（P2）

def test_bot_name_rule_matches_dashboard_1337():
    """bot 规则：`bot` / `*_bot` / `*-bot`；abbot / robot / talbot 是真人不能误杀。"""
    for name in ("grape_bot", "svc-bot", "bot", "Grape_BOT"):
        assert eff.is_bot_name(name), name
    for name in ("abbot", "robot", "talbot", "botanist"):
        assert not eff.is_bot_name(name), name
    # 名单命中（非 bot 形态的机器人靠名单）
    assert eff.is_bot_name("svc-account", ["svc-account"])
    assert not eff.is_bot_name("", ["svc-account"])


def test_wip_distribution_grouping_order_and_total():
    """归属 created_by 优先→assignee→user_id；人类降序 → bot 灰行 → 未分配垫底；
    合计必须等于 ③ 在制工作项数（同源同口径）。"""
    conn, c = _cursor()
    _seed(c, [
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "", "assignee": "bob"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "grape_bot"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "", "assignee": ""},
        # 已关闭不进在办
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice",
         "status": "completed", "completed_at": "2026-11-02 09:00:00"},
        # 子任务不进（build_board 的 SQL 已过滤，这里模拟过滤后的行）
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    wip = eff.build_wip_distribution(rows, bot_names=("grape_bot",))
    assert wip["total"] == 6, wip
    assert [i["name"] for i in wip["items"]] == ["alice", "bob", "grape_bot", eff.UNASSIGNED_LABEL], wip
    assert [i["count"] for i in wip["items"]] == [3, 1, 1, 1], wip
    assert [i["kind"] for i in wip["items"]] == ["human", "human", "bot", "unassigned"], wip
    assert wip["human_total"] == 4 and wip["bot_total"] == 1 and wip["unassigned_total"] == 1
    # 已完成按同一归属链计数：alice 那 1 条 completed 要出现在 alice 行上
    assert wip["items"][0]["completed"] == 1, wip["items"]
    assert wip["items"][1]["completed"] == 0
    assert wip["completed_total"] == 1
    assert wip["top2_share"] == pytest.approx(4 / 6)
    assert sum(i["share"] for i in wip["items"]) == pytest.approx(1.0)


def test_wip_falls_back_to_uid_mapping():
    conn, c = _cursor()
    c.execute("CREATE TABLE unified_user_mapping (user_id INTEGER, gitcode_user TEXT, github_login TEXT)")
    c.execute("INSERT INTO unified_user_mapping VALUES (7,'carol','')")
    c.connection.commit()
    _seed(c, [{"created_at": "2026-11-01 09:00:00", "created_by": "", "assignee": "", "user_id": 7}])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    uid_names = eff.load_uid_names(c, [7])
    assert uid_names == {7: "carol"}
    wip = eff.build_wip_distribution(rows, uid_names=uid_names)
    assert wip["items"][0]["name"] == "carol"
    assert wip["unassigned_total"] == 0


def test_wip_total_equals_aging_open_in_board():
    """⑨ 与 ③ 必须同源同数——两处口径漂移是最容易被用户发现的那类 bug。"""
    conn, c = _cursor()
    _seed(c, [
        {"created_at": "2026-10-01 09:00:00", "created_by": "alice"},
        {"created_at": "2026-10-01 09:00:00", "created_by": "bob"},
        {"created_at": "2026-11-08 09:00:00", "created_by": ""},
    ])
    out = eff.build_board(c, ["gitcode:org/repo"], repo_label="org/repo", now=NOW,
                          bot_names=("grape_bot",))
    assert out["wip_by_assignee"]["total"] == out["aging"]["open"] == \
        out["summary"]["open_total"] == 3, out["wip_by_assignee"]

# ------------------------------------------------------------- ⑩ 任务交付时长（P3）

def _seed_task(c, status, started, completed, agent_type="coding", repo="gitcode:org/repo"):
    c.execute(
        "INSERT INTO agent_tasks (status, started_at, completed_at, agent_type, repo_key)"
        " VALUES (?,?,?,?,?)", (status, started, completed, agent_type, repo))
    c.connection.commit()


def test_task_delivery_grouping_and_units():
    """all 组 = 全集；failed 组独立出数（失败任务几乎立即失败，混在一起会被压平）。"""
    conn, c = _cursor()
    c.execute("CREATE TABLE agent_tasks (status TEXT, started_at TEXT, completed_at TEXT,"
              " agent_type TEXT, repo_key TEXT)")
    # 2 分钟 / 4 分钟 completed；10 秒 failed；无 completed_at 的 queued 不计
    _seed_task(c, "completed", "2026-11-09T01:00:00+00:00", "2026-11-09T01:02:00+00:00", "coding")
    _seed_task(c, "completed", "2026-11-09T01:00:00+00:00", "2026-11-09T01:04:00+00:00", "qa")
    _seed_task(c, "failed", "2026-11-09T01:00:00+00:00", "2026-11-09T01:00:10+00:00", "coding")
    _seed_task(c, "queued", "2026-11-09T01:00:00+00:00", "", "sre")
    rows = c.execute("SELECT status, started_at, completed_at, agent_type FROM agent_tasks").fetchall()

    allg = eff.build_task_delivery(rows)
    assert allg["group_by"] == "all"
    assert allg["sample"] == 3, allg           # queued 无 completed_at 不进样本
    assert allg["total_tasks"] == 4, allg      # 但任务总数如实反映
    assert allg["p50_min"] == 2.0, allg        # [2, 4, 0.167] 分钟的 nearest-rank 中位
    assert allg["p90_min"] == 4.0, allg
    assert set(allg["by_status"]) == {"completed", "failed"}
    assert allg["by_status"]["completed"]["sample"] == 2
    assert allg["by_status"]["failed"]["sample"] == 1
    assert allg["by_agent_type"]["coding"]["sample"] == 2
    assert allg["by_agent_type"]["qa"]["sample"] == 1

    failed = eff.build_task_delivery(rows, group_by="failed")
    assert failed["sample"] == 1
    assert failed["p50_min"] == round(10 / 60.0, 2), failed
    assert failed["by_status"]["completed"]["sample"] == 2   # 拆分不受 group_by 影响


def test_task_delivery_skips_dirty_and_timezone_neutral():
    """completed_at < started_at 进 dirty_skipped；UTC 时间相减不受时区影响。"""
    conn, c = _cursor()
    c.execute("CREATE TABLE agent_tasks (status TEXT, started_at TEXT, completed_at TEXT,"
              " agent_type TEXT, repo_key TEXT)")
    _seed_task(c, "completed", "2026-11-09T01:00:00+00:00", "2026-11-09T00:00:00+00:00")
    _seed_task(c, "completed", "2026-11-09T01:00:00+00:00",
               "2026-11-09T03:00:00+00:00")   # 2 小时
    rows = c.execute("SELECT status, started_at, completed_at, agent_type FROM agent_tasks").fetchall()
    td = eff.build_task_delivery(rows)
    assert td["sample"] == 1, td
    assert td["dirty_skipped"] == 1, td
    assert td["p50_min"] == 120.0, td


def test_board_includes_task_delivery_section():
    conn, c = _cursor()
    c.execute("CREATE TABLE agent_tasks (status TEXT, started_at TEXT, completed_at TEXT,"
              " agent_type TEXT, repo_key TEXT)")
    _seed(c, [{"created_at": "2026-11-09 09:00:00", "created_by": "alice"}])
    _seed_task(c, "completed", "2026-11-09T01:00:00+00:00", "2026-11-09T01:01:00+00:00")
    out = eff.build_board(c, ["gitcode:org/repo"], repo_label="org/repo", now=NOW)
    td = out["task_delivery"]
    assert td["sample"] == 1, out.keys()
    assert td["by_status"]["completed"]["sample"] == 1
    assert "p50_min" in td and "buckets" in td

# ------------------------------------------------------------- 下钻：周→日 / 桶→issue（第三轮）

def _detail_cursor():
    """带 issue 详情列 + 场景表的内存库。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE issue (issue_number INTEGER, title TEXT, status TEXT, stage TEXT,"
        " scene_id TEXT, created_at TEXT, completed_at TEXT, created_by TEXT,"
        " assignee TEXT, user_id INTEGER, repo_key TEXT, is_subtask_mode INTEGER)")
    conn.execute(
        "CREATE TABLE devops_pipeline_scene (scene_id TEXT, name TEXT, display_name TEXT)")
    return conn, conn.cursor()


def _seed_detail(c, rows):
    for r in rows:
        c.execute(
            "INSERT INTO issue (issue_number, title, status, stage, scene_id, created_at,"
            " completed_at, created_by, assignee, user_id, repo_key, is_subtask_mode)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (r["number"], r.get("title", "t"), r.get("status", "processing"),
             r.get("stage", "vod"), r.get("scene_id", ""), r["created_at"],
             r.get("completed_at", ""), r.get("created_by", "alice"),
             r.get("assignee", ""), 0, "gitcode:org/repo"))
    c.connection.commit()


def test_parse_week_label():
    assert eff.parse_week_label("2026-W45") == datetime(2026, 11, 2)
    assert eff.parse_week_label("W45") == datetime.fromisocalendar(
        datetime.now().isocalendar()[0], 45, 1)
    assert eff.parse_week_label("2026-W60") is None
    assert eff.parse_week_label("garbage") is None
    assert eff.parse_week_label("") is None


def test_daily_week_sum_equals_weekly_bucket():
    """周→日下钻必须与周桶对得上：该周 7 天之和 == 该周的周值。"""
    conn, c = _detail_cursor()
    _seed_detail(c, [
        {"number": 1, "created_at": "2026-11-03 10:00:00"},                       # 周二
        {"number": 2, "created_at": "2026-11-09 10:00:00"},                       # 下周一
        {"number": 3, "created_at": "2026-11-06 10:00:00", "status": "completed",
         "completed_at": "2026-11-08 10:00:00"},                                  # 周日完成
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    wk45 = datetime(2026, 11, 2)
    daily = eff.build_daily(rows, wk45)
    weekly = eff.build_throughput(rows, NOW, weeks=12)
    idx = weekly["week_labels_full"].index("2026-W45")
    assert daily["totals"]["created"] == weekly["created"][idx] == 2
    assert daily["totals"]["completed"] == weekly["completed"][idx] == 1
    assert len(daily["days"]) == 7
    assert daily["days"][2]["date"] == "2026-11-04"
    assert daily["days"][6]["backlog"] == weekly["backlog"][idx]


def test_issue_list_buckets_match_aging_and_cycle():
    """桶下钻的条数必须等于卡片上该桶的数字（同源同桶边界）。"""
    conn, c = _detail_cursor()
    _seed_detail(c, [
        {"number": 1, "created_at": (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")},
        {"number": 2, "created_at": (NOW - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S")},
        {"number": 3, "created_at": (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")},
        {"number": 4, "created_at": (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
         "status": "completed",
         "completed_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    aging = eff.build_aging(rows, NOW, threshold_days=7)
    bucket_counts = {b["label"]: b["count"] for b in aging["buckets"]}

    for label in (">2周", "1-2周", "8-24小时"):
        out = eff.build_issue_list(c, ["gitcode:org/repo"], kind="aging",
                                  bucket=label, now=NOW)
        assert out["total"] == bucket_counts[label], (label, out["total"], bucket_counts)

    overdue = eff.build_issue_list(c, ["gitcode:org/repo"], kind="overdue",
                                  threshold_days=7, now=NOW)
    assert overdue["total"] == aging["over_threshold"]

    cycle = eff.build_issue_list(c, ["gitcode:org/repo"], kind="cycle",
                                bucket="<1天", now=NOW)
    assert cycle["total"] == 1 and cycle["items"][0]["number"] == 4
    assert cycle["items"][0]["metric_unit"] == "hours"


def test_issue_list_reports_scene_and_falls_back_to_raw_id():
    conn, c = _detail_cursor()
    c.execute("INSERT INTO devops_pipeline_scene VALUES ('default','通用开发','通用开发')")
    c.connection.commit()
    _seed_detail(c, [
        {"number": 1, "created_at": (NOW - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
         "scene_id": "default"},
        {"number": 2, "created_at": (NOW - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
         "scene_id": "unknown-scene"},
    ])
    out = eff.build_issue_list(c, ["gitcode:org/repo"], kind="aging", bucket=">2周", now=NOW)
    scenes = {i["number"]: i["scene"] for i in out["items"]}
    assert scenes[1] == "通用开发"
    assert scenes[2] == "unknown-scene"      # 场景表查不到时回退原始 id，不静默编造


def test_issue_list_unknown_kind_and_empty_repo():
    conn, c = _detail_cursor()
    _seed_detail(c, [{"number": 1, "created_at": "2026-11-01 10:00:00"}])
    bad = eff.build_issue_list(c, ["gitcode:org/repo"], kind="nope", now=NOW)
    assert bad.get("error") == "unknown_kind"
    empty = eff.build_issue_list(c, [], kind="aging", now=NOW)
    assert empty["empty"] is True and empty["total"] == 0

def test_wip_sorted_by_total_and_bar_length_variable():
    """排序口径 = 总条数（未关闭+已完成），与条形长度一致；
    只有未关闭多但完成少的人不应排在总条数更少的人前面。"""
    conn, c = _cursor()
    _seed(c, [
        # alice: 1 未关闭 + 3 已完成 = 4
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice", "status": "completed",
         "completed_at": "2026-11-02 09:00:00"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice", "status": "completed",
         "completed_at": "2026-11-02 09:00:00"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "alice", "status": "completed",
         "completed_at": "2026-11-02 09:00:00"},
        # bob: 3 未关闭 + 0 已完成 = 3
        {"created_at": "2026-11-01 09:00:00", "created_by": "bob"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "bob"},
        {"created_at": "2026-11-01 09:00:00", "created_by": "bob"},
        # carol: 0 未关闭 + 1 已完成 → 不进在办分布（WIP 口径只看有在办的人）
        {"created_at": "2026-11-01 09:00:00", "created_by": "carol", "status": "completed",
         "completed_at": "2026-11-02 09:00:00"},
    ])
    rows = c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        " FROM issue").fetchall()
    wip = eff.build_wip_distribution(rows)
    assert [i["name"] for i in wip["items"]] == ["alice", "bob"], wip["items"]
    assert [i["total"] for i in wip["items"]] == [4, 3]
    assert all(i["name"] != "carol" for i in wip["items"])   # 无在办的人不进 WIP 分布
    # 未关闭数仍然单独给：bob 的 unfixed 数更高但总条数更低
    assert [i["open"] for i in wip["items"]] == [1, 3]

def test_issue_list_pagination_offsets_without_overlap():
    """分页：total 恒定、页间不重不漏、越界页返回空但 total 仍在。"""
    conn, c = _detail_cursor()
    _seed_detail(c, [
        {"number": i, "created_at": (NOW - timedelta(days=20 + i)).strftime("%Y-%m-%d %H:%M:%S")}
        for i in range(1, 46)
    ])
    keys = ["gitcode:org/repo"]
    p1 = eff.build_issue_list(c, keys, kind="aging", bucket=">2周", limit=20, offset=0, now=NOW)
    p2 = eff.build_issue_list(c, keys, kind="aging", bucket=">2周", limit=20, offset=20, now=NOW)
    p3 = eff.build_issue_list(c, keys, kind="aging", bucket=">2周", limit=20, offset=40, now=NOW)
    assert p1["total"] == p2["total"] == p3["total"] == 45
    assert len(p1["items"]) == 20 and len(p2["items"]) == 20 and len(p3["items"]) == 5
    nums1 = [i["number"] for i in p1["items"]]
    nums2 = [i["number"] for i in p2["items"]]
    nums3 = [i["number"] for i in p3["items"]]
    assert not (set(nums1) & set(nums2)) and not (set(nums2) & set(nums3))
    assert set(nums1 + nums2 + nums3) == set(range(1, 46))     # 不重不漏
    # 稳定排序：同一页两次取结果一致
    again = eff.build_issue_list(c, keys, kind="aging", bucket=">2周", limit=20, offset=20, now=NOW)
    assert [i["number"] for i in again["items"]] == nums2
    beyond = eff.build_issue_list(c, keys, kind="aging", bucket=">2周", limit=20, offset=200, now=NOW)
    assert beyond["items"] == [] and beyond["total"] == 45

